from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, ClassVar

import yaml
from jsonschema import Draft202012Validator, SchemaError

from agentic_hil.comports import data_result, decode_bytes
from agentic_hil.config import (
    ConfigError,
    UniqueKeyLoader,
    absolute_without_symlinks,
    bind_debugger,
    is_path_within_frozen,
    reject_nonfinite_numbers,
    safe_read_text,
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
from agentic_hil.report import audit_errors, overall_success
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import AgenticHILConfig, DebuggerConfig, JsonObject

DEFAULT_TEST_CONFIG_PATH = ".agentic-hil/testconfig.yaml"
TEST_CONFIG_SCHEMA_RESOURCE = "schemas/testconfig.schema.json"
# The annotation the plan schema marks a newer format's additions with. A plan
# is held to what its own `version:` contains, and the schema is what says which
# version each action and each key arrived in.
PLAN_FEATURE_VERSION_KEY = "x-since-version"
# What a `uart_expect` that timed out answers with: the last of what the line
# actually said. Capped, because a chatty port must not turn one red step into an
# unbounded result — but never zero, because a red step that cannot say what the
# port did say is the failure an operator has to reproduce by hand.
UART_EXPECT_TAIL_BYTES = 512
# How long a `uart_expect` pass that read nothing waits before the next one.
# Reached only when `com_read` returned early — a session that is no longer
# active ends its wait at once — so this paces a line that has stopped talking
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
# two-sentence shape as the v2 table above and for the same reason — a plan that
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
# The longest a `delay` step may wait, in milliseconds. Ten minutes: a plan that
# must wait longer than that is waiting for something it should be expecting
# instead, and an unbounded wait in a plan is how a bench ends up held by a run
# nobody is watching.
DELAY_MAX_MS = 600_000


def decoded_equals(decoded: str, expected: str) -> bool:
    """True when what the line said equals `expected` — not merely contains it.

    A serial line has no end, so "the whole thing" is read two ways and either
    counts: the whole of what has been received so far, or any one complete line
    of it, once surrounding whitespace is removed. The first is what a board that
    answers once and stops looks like; the second is what a board that prints its
    banner in a loop looks like, where the whole buffer is never one value."""
    if decoded.strip() == expected:
        return True
    lines = decoded.splitlines()
    if lines and not decoded.endswith(("\n", "\r")):
        # The last fragment has no terminator yet, so it is not a line — it is
        # the beginning of one, and matching it would be matching a prefix.
        lines = lines[:-1]
    return any(line.strip() == expected for line in lines)


class StepComparator:
    """What a feedback step claims about what it received.

    An object in the plan (`comparator: {equals: ...}`) rather than a bare value,
    so the preprocessing a bench eventually needs — scaling a raw count into a
    unit, converting a base — can be added as further keys without invalidating
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

    @property
    def claim(self) -> str:
        if self.bounds is not None:
            return "range"
        return "equals" if self.equals is not None else "pattern"

    @property
    def written(self) -> str:
        """The plan's own string, which sizes the read window."""
        return self.equals or self.pattern or ""

    def matches(self, decoded: str) -> bool:
        if self.equals is not None:
            return decoded_equals(decoded, self.equals)
        if self._finditer is None:
            return False
        if self.bounds is None:
            return next(self._finditer(decoded), None) is not None
        low, high = self.bounds
        matched = False
        # Every match, not the first: a line that reported an out-of-range value
        # and then an in-range one is a bench that settled, and searching only
        # the earliest occurrence would keep answering with the older reading.
        for match in self._finditer(decoded):
            try:
                value = float(match.group(1))
            except ValueError:
                continue
            self.captured_text, self.captured_value = match.group(1), value
            matched = matched or low <= value <= high
        return matched

    def report(self) -> JsonObject:
        """What the result says about this comparator, met or unmet."""
        if self.bounds is None:
            return {"comparator": self.raw}
        return {"comparator": self.raw, "captured_text": self.captured_text, "captured_value": self.captured_value}


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

    @property
    def tail_result(self) -> JsonObject:
        return {"received_tail": data_result(self.tail, self.encoding), "received_tail_truncated": self.bytes_received > len(self.tail)}
# What a step means is answered by one class per device kind (see StepDevice
# below), and ACTION_SCHEMAS / ROUTE_FIELDS are merged from those classes rather
# than written out here. Routing used to be a two-way split held in two
# constants — "UART or debugger?" in one, "which actions need a probe?" in
# another — and the second was already documented as the thing that would go
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
    # configures exactly one probe. A step names its device once — an alias
    # beside `device:` is refused at preflight rather than silently preferred.
    debugger: str | None = None
    port_id: str | None = None
    bus_id: str | None = None

    @property
    def route(self) -> str:
        return next((named for field_name in ROUTE_FIELDS if (named := getattr(self, field_name))), "-")

    @property
    def route_keys(self) -> list[str]:
        """Every routing key this step actually set."""
        return [field_name for field_name in ROUTE_FIELDS if getattr(self, field_name) is not None]


@dataclass
class PlanState:
    """What a plan has already done to the bench, at the step preflight is on.

    Carried by the walk rather than by device objects, because preflight must
    build nothing: a plan that fails validation has opened no service, taken no
    lock and created no directory, and the surest way to keep that true is for
    preflight to have nowhere to put such a thing. It also makes preflight
    repeatable — the same plan asked twice gets the same answer."""

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
        loaded = yaml.load(safe_read_text(path, workspace=base), Loader=UniqueKeyLoader)
    except FileNotFoundError as error:
        raise ConfigError("test_config_not_found", "Test reactor configuration file could not be found.", {"path": str(path)}) from error
    except OSError as error:
        raise ConfigError(
            "test_config_unreadable",
            "Test reactor configuration file could not be read.",
            {"path": str(path), "backend_error": str(error)},
        ) from error
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
    steps = [
        TestStep(
            action=str(step["action"]),
            arguments={key: value for key, value in step.items() if key not in {"action", *ROUTE_FIELDS}},
            **{name: None if step.get(name) is None else str(step[name]) for name in ROUTE_FIELDS},
        )
        for step in raw["steps"]
    ]
    return TestConfig(path=str(path), name=str(raw.get("name") or path.stem), steps=steps)


def reject_superseded_plan_version(raw: JsonObject, path: str | None = None) -> None:
    """Refuse a version 1 plan by name.

    Version 1 steps addressed a `device:` naming a single modelled unit the
    config no longer has — not the `device:` of version 3, which names one entry
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
    schema = test_config_schema()
    for index, step in enumerate(raw["steps"]):
        definition = schema["$defs"][ACTION_SCHEMAS[step["action"]]]
        introduced = definition.get(PLAN_FEATURE_VERSION_KEY)
        if isinstance(introduced, int) and version < introduced:
            raise_outdated_plan_version(path, f"steps[{index}].action", step["action"], version, introduced)
        for key in step:
            property_schema = resolve_schema_node(definition.get("properties", {}).get(key), schema)
            introduced = property_schema.get(PLAN_FEATURE_VERSION_KEY)
            if isinstance(introduced, int) and version < introduced:
                raise_outdated_plan_version(path, f"steps[{index}].{key}", key, version, introduced)


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
    root_schema = deepcopy(schema)
    root_schema["properties"]["steps"]["items"] = {}
    errors = sorted(Draft202012Validator(root_schema).iter_errors(raw), key=lambda item: list(item.absolute_path))
    if errors:
        raise_test_config_validation_error(errors[0], path)

    for index, step in enumerate(raw["steps"]):
        if not isinstance(step, dict):
            raise ConfigError(
                "test_config_invalid",
                "Test reactor step must be a mapping.",
                {"path": path, "field": f"steps[{index}]", "value": step},
            )
        action = step.get("action")
        if action not in ACTION_SCHEMAS:
            raise ConfigError(
                "test_config_invalid",
                "Test reactor action has an unsupported value.",
                {
                    "path": path,
                    "field": f"steps[{index}].action",
                    "value": action,
                    "allowed_values": list(ACTION_SCHEMAS),
                },
            )
        step_schema = {**schema["$defs"][ACTION_SCHEMAS[action]], "$defs": schema["$defs"]}
        step_errors = sorted(Draft202012Validator(step_schema).iter_errors(step), key=lambda item: list(item.absolute_path))
        if step_errors:
            raise_test_config_validation_error(step_errors[0], path, ["steps", str(index)])


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
        # A step whose alternatives are "exactly one of these keys" — the shape
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
    # reads its arguments itself — a deadline and an expectation are not
    # `com_read` arguments — and builds the call it makes.
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
    more than once to one method, it declares that method under each name — which
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
    rather than being shadowed by it, and an action declared once on the base —
    `delay` — is served by every kind without any of them repeating it."""
    collected: dict[str, StepActionSpec] = {}
    for ancestor in reversed(device_class.__mro__):
        for attribute in vars(ancestor).values():
            for spec in getattr(attribute, STEP_ACTION_DECLARATIONS, ()):
                collected[spec.name] = spec
    return collected


@lru_cache(maxsize=1)
def test_config_schema() -> JsonObject:
    """The bundled plan schema, read once. Callers must not mutate it."""
    return json.loads(resources.files("agentic_hil").joinpath(TEST_CONFIG_SCHEMA_RESOURCE).read_text(encoding="utf-8"))


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
    answers what a unit *is* — its hardware-derived lock key, its tool
    vocabulary, its mutex — and it is imported by the backends themselves; a plan
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

    def __init__(self, config_id: str, device: Device, service: AgenticHILToolService):
        self.id = config_id
        self.device = device
        self.service = service
        self.cleanup_interrupts: list[BaseException] = []
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
    def name_refusal(cls, reactor: TestReactor, index: int, step: TestStep) -> JsonObject | None:
        entries = cls.config_entries(reactor.config)
        name = cls.step_config_id(reactor.config, step)
        if name is None:
            return cls.unnamed_refusal(reactor, index, step)
        if name not in entries:
            return preflight_error(index, step, cls.route_field, cls.unknown_name_summary, {cls.configured_field: sorted(entries)})
        return None

    @classmethod
    def unnamed_refusal(cls, reactor: TestReactor, index: int, step: TestStep) -> JsonObject | None:
        """A step that named no entry. Only a debugger may leave its route field
        out, and only while the project configures exactly one probe; the schema
        requires every other kind's."""
        return preflight_error(index, step, cls.route_field, cls.unknown_name_summary, {cls.configured_field: sorted(cls.config_entries(reactor.config))})

    # --- plan time -------------------------------------------------------

    @classmethod
    def preflight(cls, reactor: TestReactor, index: int, step: TestStep, state: PlanState) -> JsonObject | None:
        """Refuse this step against the config and what the plan has already done,
        or None. Runs for every step before the first one executes."""
        return None

    @classmethod
    def tool_calls(cls, config: AgenticHILConfig, step: TestStep) -> list[tuple[str, JsonObject]]:
        """The plan-supplied tool calls a step will make, as (tool, arguments),
        so preflight can put them through the exact MCP contract validators the
        dispatcher enforces — a step this reactor's schema accepted but the tool
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
        declare is `not_supported` naming the kind by construction — there is no
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
        kind of thing it is. It is still a step — it routes to a named device and
        it appears in the run's result — because a wait that left no trace would
        be a gap in the record of what a test did."""
        duration_ms = int(step.arguments["duration_ms"])
        time.sleep(duration_ms / 1000.0)
        return {
            "ok": True,
            "tool": "test_reactor",
            "summary": "Test plan waited.",
            self.route_field: self.id,
            "action": "delay",
            "duration_ms": duration_ms,
        }

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
# subclasses, so the base's own collection is made here — one call, the same one
# every kind gets.
StepDevice.step_action_specs = collect_step_actions(StepDevice)
StepDevice.step_actions = {name: spec.schema for name, spec in StepDevice.step_action_specs.items()}


class SessionDevice(StepDevice):
    """A device a plan opens and closes: a serial line, a CAN bus.

    The two differ in their permissions and in the traffic they carry, not in
    their session — both can be left open by a plan that fails, both may only be
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

    def __init__(self, config_id: str, device: Device, service: AgenticHILToolService):
        super().__init__(config_id, device, service)
        # True while this plan holds the session it opened.
        self._owns_session = False

    @classmethod
    def preflight(cls, reactor: TestReactor, index: int, step: TestStep, state: PlanState) -> JsonObject | None:
        name = str(cls.step_config_id(reactor.config, step))
        refusal = cls.permission_refusal(reactor, index, step, cls.config_entries(reactor.config)[name])
        if refusal is not None:
            return refusal
        key = (cls.kind, name)
        if step.action == cls.open_action:
            if key in state.open_sessions:
                return preflight_error(index, step, "action", f"{cls.session_noun} session is already open in this test plan.")
            state.open_sessions.add(key)
            return None
        if key not in state.open_sessions:
            closing = step.action == cls.close_action
            return preflight_error(index, step, "action", f"{cls.session_noun} session must be opened before {'it can be closed' if closing else 'this action'}.")
        if step.action == cls.close_action:
            state.open_sessions.discard(key)
        return None

    @classmethod
    def permission_refusal(cls, reactor: TestReactor, index: int, step: TestStep, entry: Any) -> JsonObject | None:
        """Refuse a step this entry's own permissions do not allow."""
        return None

    def open_session(self, step: TestStep) -> JsonObject:
        """Open, and take ownership of what this plan opened. The body of every
        kind's `role="open"` action."""
        result = self.call_tool(step)
        self._owns_session = result.get("ok") is True and not result.get("already_active", False)
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
        """Serial stimulus, straight through `com_write` — the same tool and the
        same permission an agent driving the line by hand would meet. A port the
        config will not let a plan write to is refused by name at preflight, so
        the plan is corrected rather than the bench half driven."""
        return self.call_tool(step)

    @step_action("uart_expect", schema="uartExpect", tool="com_read", forwards_arguments=False)
    def _uart_expect(self, step: TestStep) -> JsonObject:
        # Exactly one of the two is present — the schema's own `oneOf` is what
        # holds that — so the first one found names both the claim and the key
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
    def permission_refusal(cls, reactor: TestReactor, index: int, step: TestStep, entry: Any) -> JsonObject | None:
        # The open is asked about reading, and that covers every action that only
        # listens: each of them requires a session this plan opened, so a port
        # the config will not let a plan read is refused before any of them.
        # Writing is its own grant and its own refusal, named where it applies.
        if step.action == cls.open_action and not reactor.config.com_read_allowed(entry):
            return preflight_error(index, step, "action", "Reading this COM port is disabled by the authoritative config.")
        if step.action == "uart_write" and not entry.permissions.allow_write:
            return preflight_error(index, step, "action", "Writing to this COM port is disabled by the authoritative config.", {"permission": "allow_write"})
        return None

    @classmethod
    def preflight(cls, reactor: TestReactor, index: int, step: TestStep, state: PlanState) -> JsonObject | None:
        refusal = cls._pattern_refusal(index, step)
        if refusal is not None:
            return refusal
        return super().preflight(reactor, index, step, state)

    @classmethod
    def _pattern_refusal(cls, index: int, step: TestStep) -> JsonObject | None:
        """Refuse a regular expression this plan cannot mean, before the run.

        A pattern that does not compile, or a `range` whose pattern captures
        nothing to put in it, is a fault in the plan and is answered here:
        preflight builds nothing, so a plan refused for either has opened no
        session and touched no board. `re.error` carries the position it gave up
        at, which is the whole reason this is not left to raise out of the run as
        a traceback."""
        if step.action == "uart_expect":
            field_name, pattern, needs_capture = "pattern", step.arguments.get("pattern"), False
            summary = "Test step's uart_expect pattern is not a valid Python regular expression."
        elif step.action == "uart_read":
            comparator = step.arguments.get("comparator") or {}
            field_name, pattern = "comparator.pattern", comparator.get("pattern")
            needs_capture = comparator.get("range") is not None
            summary = "Test step's comparator pattern is not a valid Python regular expression."
        else:
            return None
        if pattern is None:
            return None
        try:
            compiled = re.compile(str(pattern))
        except re.error as error:
            return preflight_error(index, step, field_name, summary, {"error_type": "invalid_argument", "value": str(pattern)[:128], "regex_error": str(error)})
        if needs_capture and compiled.groups != 1:
            return preflight_error(
                index,
                step,
                field_name,
                "A `range` comparator reads its value out of the pattern's one capture group, so the pattern must have exactly one.",
                {"error_type": "invalid_argument", "value": str(pattern)[:128], "capture_groups": compiled.groups},
            )
        return None

    # --- reading the line ------------------------------------------------

    @staticmethod
    def _matcher(field_name: str, expected: str) -> Callable[[str], bool]:
        """What counts as a match for one of the two v2 expectation kinds.

        A pattern is `re.search`, so it is anchored where it is written and
        nowhere else: a plan that means "the line starts this way" writes the
        `^` itself. Compiled once per step rather than per read — the pattern
        already compiled cleanly at preflight, so this cannot raise."""
        if field_name == "text":
            return lambda decoded: expected in decoded
        search = re.compile(expected).search
        return lambda decoded: search(decoded) is not None

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
        """Read until the comparator is met, or fail saying what was received —
        and, for a range, which value was captured against which bounds."""
        met, unmet = COMPARATOR_SUMMARIES[comparator.claim]
        outcome = self._read_until(timeout_s, comparator.written, comparator.matches)
        if outcome.read_failure is not None:
            return outcome.read_failure
        common = {"port_id": self.id, **comparator.report(), "timeout_s": timeout_s, "bytes_received": outcome.bytes_received, "reads": outcome.reads}
        if outcome.matched:
            return {"ok": True, "tool": "test_reactor", "summary": met, **common}
        return {"ok": False, "tool": "test_reactor", "error_type": "comparator_unmet", "summary": unmet, **common, **outcome.tail_result}

    def _read_until(self, timeout_s: float, written: str, matches: Callable[[str], bool]) -> UartReadOutcome:
        """Read this line until `matches` is satisfied or the deadline passes.

        The plan's own read path and nothing else: each pass is one `com_read` —
        the tool an agent would call by hand — waiting out whatever is left of
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
            if matches(decode_bytes(bytes(received), encoding)):
                return UartReadOutcome(True, reads, received_bytes, bytes(received[-UART_EXPECT_TAIL_BYTES:]), encoding)
            if time.monotonic() >= deadline:
                return UartReadOutcome(False, reads, received_bytes, bytes(received[-UART_EXPECT_TAIL_BYTES:]), encoding)
            if not chunk:
                # A read that returned nothing while time was left had its own
                # wait cut short — `com_read` ends its wait at once on a session
                # that is no longer active — so pace the next pass rather than
                # spinning on a line that has stopped talking.
                time.sleep(min(UART_EXPECT_IDLE_POLL_S, max(0.0, deadline - time.monotonic())))


class CanRunner(SessionDevice):
    """A configured CAN bus, as a plan opens it, drives it and closes it.

    The action set is the CAN tool surface and nothing beyond it: open, close,
    send one classic frame, read frames. Each permission refusal below is the one
    the backend would raise, moved to where a plan can still be corrected — a
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

    @step_action("can_read", schema="canRead", tool="can_read")
    def _can_read(self, step: TestStep) -> JsonObject:
        return self.call_tool(step)

    @classmethod
    def permission_refusal(cls, reactor: TestReactor, index: int, step: TestStep, entry: Any) -> JsonObject | None:
        readable = reactor.config.can_read_allowed(entry)
        if step.action == cls.open_action:
            if not readable and not entry.permissions.allow_write:
                return preflight_error(index, step, "action", "Reading and writing this CAN bus are disabled by the authoritative config.")
            if step.arguments.get("clear_rx_queue", True) and not readable:
                return preflight_error(index, step, "clear_rx_queue", "Clearing this CAN bus receive queue requires permissions.allow_read on the bus.", {"permission": "allow_read"})
            return None
        if step.action == "can_read" and not readable:
            return preflight_error(index, step, "action", "Reading this CAN bus is disabled by the authoritative config.", {"permission": "allow_read"})
        if step.action != "can_send":
            return None
        if not entry.permissions.allow_write:
            return preflight_error(index, step, "action", "Writing to this CAN bus is disabled by the authoritative config.", {"permission": "allow_write"})
        if entry.listen_only:
            # `listen_only: true` is not an obstacle standing in the way of the
            # send: it is the claim that observing this bus sends nothing, which
            # is the bus this plan itself declared. A controller held to it emits
            # no dominant bit, so the send could never leave it — and the adapter
            # settles the mode when the session opens, long before the send step
            # would be reached. Refusing the plan says which of its two halves to
            # change; refusing at the bench would only say the frame did not go.
            return preflight_error(
                index,
                step,
                "action",
                "This CAN bus is configured `listen_only: true` — the claim that observing it sends nothing — so a plan cannot also send on it. Send on a bus configured `listen_only: false`, or drop the send step.",
                {"listen_only": True, "config_field": f"can_buses.{cls.step_config_id(reactor.config, step)}.listen_only"},
            )
        return None


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
    # This kind's own actions that require a live debug session, and therefore a
    # typed-debug backend. Internal to the class that serves them, not a second
    # answer to "which device kind runs this action".
    debug_session_actions: ClassVar[frozenset[str]] = frozenset({"debug_start", "run_until_breakpoint", "dump_memory", "debug_stop"})

    def __init__(self, config_id: str, device: Device, service: AgenticHILToolService):
        super().__init__(config_id, device, service)
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
    def unnamed_refusal(cls, reactor: TestReactor, index: int, step: TestStep) -> JsonObject | None:
        """Resolve which probe a debugger step runs on, or refuse the plan.

        A plan that omits the name while several probes are configured is not
        defaulted to one: picking a board for the author is how the wrong board
        gets flashed."""
        if not reactor.config.debuggers:
            return preflight_error(index, step, "debugger", "The authoritative config declares no debuggers, so this step cannot run.")
        return preflight_error(index, step, "debugger", "Name the debugger this step runs on; the authoritative config declares several.", {"configured_debuggers": sorted(reactor.config.debuggers)})

    @classmethod
    def name_refusal(cls, reactor: TestReactor, index: int, step: TestStep) -> JsonObject | None:
        refusal = super().name_refusal(reactor, index, step)
        if refusal is not None:
            return refusal
        if cls.step_config_id(reactor.config, step) != reactor.config.debugger_id and reactor._service_factory is None:
            return preflight_error(index, step, "debugger", "Running a step on another debugger needs a service factory to build that probe's service; the reactor would otherwise execute it on the bound probe.", {"bound_debugger": reactor.config.debugger_id})
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
    def preflight(cls, reactor: TestReactor, index: int, step: TestStep, state: PlanState) -> JsonObject | None:
        config = reactor.config
        debugger_id = str(cls.step_config_id(config, step))
        debugger = config.debuggers[debugger_id]
        permissions = debugger.permissions

        if step.action == "flash":
            if state.debug_session is not None:
                return preflight_error(index, step, "action", "Firmware cannot be flashed while a debug session is active.", {"debug_session_debugger": state.debug_session})
            if not permissions.allow_flash:
                return preflight_error(index, step, "action", "Flashing is disabled for this debugger by the authoritative config.")
            if step.arguments.get("reset_after_flash", False) and not permissions.allow_reset:
                return preflight_error(index, step, "reset_after_flash", "Post-flash reset is disabled for this debugger by the authoritative config.")
            if permissions.allow_raw_debugger_commands:
                return preflight_error(index, step, "action", "Flashing is disabled while this debugger allows raw debugger commands.", {"permission": "allow_raw_debugger_commands"})
            if permissions.allow_mass_erase:
                return preflight_error(index, step, "action", "Flashing is disabled while this debugger allows mass erase.", {"permission": "allow_mass_erase"})
            return cls._artifact_refusal(reactor, index, step, require_elf=False)

        if step.action == "reset":
            # The two answers flash already gives, for the same reasons.
            # `reset_target` is a one-shot that cannot take the lease a live
            # debug session holds, so a reset inside one could only ever come
            # back busy; and the permission that governs it is the one the
            # config names for this probe. Which resets a backend can actually
            # perform is deliberately not asked here: a mode it will not run —
            # `init` off OpenOCD — is refused by that backend with its own
            # `not_supported`, and that refusal is the honest answer.
            if state.debug_session is not None:
                return preflight_error(index, step, "action", "The target cannot be reset while a debug session is active.", {"debug_session_debugger": state.debug_session})
            if not permissions.allow_reset:
                return preflight_error(index, step, "action", "Target reset is disabled for this debugger by the authoritative config.")
            return None

        if step.action in cls.debug_session_actions and debugger.type != "openocd":
            return preflight_error(
                index,
                step,
                "action",
                "Typed debug actions currently require a debugger of type 'openocd'.",
                {"debugger_type": debugger.type},
            )
        if step.action == "debug_start":
            if state.debug_session is not None:
                return preflight_error(index, step, "action", "A debug session is already active in this test plan.", {"debug_session_debugger": state.debug_session})
            mode = str(step.arguments.get("mode", "attach"))
            # Preflight is the only diagnosis the operator gets: it runs
            # before any tool call, so the backend's per-flag messages are
            # never reached. Name the flag that actually fired rather than
            # pointing at a permission the config plainly shows as false.
            if not config.probe_allowed(debugger):
                return preflight_error(index, step, "action", "Debug sessions require allow_probe on this debugger.", {"permission": "allow_probe"})
            if permissions.allow_raw_debugger_commands:
                return preflight_error(index, step, "action", "Debug sessions are disabled while this debugger allows raw debugger commands.", {"permission": "allow_raw_debugger_commands"})
            if mode != "attach" and not permissions.allow_reset:
                return preflight_error(index, step, "mode", f"Debug mode '{mode}' requires allow_reset on this debugger.", {"permission": "allow_reset"})
            if mode == "load" and not permissions.allow_flash:
                return preflight_error(index, step, "mode", "Debug load mode requires allow_flash on this debugger.", {"permission": "allow_flash"})
            if mode == "load" and permissions.allow_mass_erase:
                return preflight_error(index, step, "mode", "Debug load mode is disabled while this debugger allows mass erase.", {"permission": "allow_mass_erase"})
            artifact_error = cls._artifact_refusal(reactor, index, step, require_elf=True)
            if artifact_error is not None:
                return artifact_error
            state.debug_session = debugger_id
            return None
        if step.action == "debug_stop":
            if state.debug_session is None:
                return preflight_error(index, step, "action", "A debug session must be started before it can be stopped.")
            if state.debug_session != debugger_id:
                return preflight_error(index, step, "debugger", "A debug session may only be stopped on the debugger that started it.", {"debug_session_debugger": state.debug_session})
            state.debug_session = None
            return None
        if state.debug_session is None:
            return preflight_error(index, step, "action", "A debug session must be started before this action.")
        if state.debug_session != debugger_id:
            return preflight_error(index, step, "debugger", "This action must run on the debugger that started the debug session.", {"debug_session_debugger": state.debug_session})
        symbol = breakpoint_symbol(step.arguments.get("location")) if step.action == "run_until_breakpoint" else step.arguments.get("symbol")
        if symbol is None and not config.debug.allow_all_symbols:
            return preflight_error(index, step, "location", "File/line breakpoints require debug.allow_all_symbols.")
        if symbol is not None and not symbol_allowed(config, str(symbol)):
            field_name = "location" if step.action == "run_until_breakpoint" else "symbol"
            return preflight_error(index, step, field_name, "Symbol is not allowed by the authoritative debug config.", {"symbol": symbol})
        if step.action == "dump_memory" and hasattr(reactor.service, "artifacts"):
            output = reactor.service.artifacts.validate_output_path(str(step.arguments["output_path"]), "debug_dump_symbol_ihex")
            if output.get("ok") is not True:
                return preflight_error(
                    index,
                    step,
                    "output_path",
                    str(output.get("summary", "Memory dump output path is invalid.")),
                    {"validation": output},
                )
        return None

    @classmethod
    def _artifact_refusal(cls, reactor: TestReactor, index: int, step: TestStep, require_elf: bool) -> JsonObject | None:
        if not hasattr(reactor.service, "artifacts"):
            return None
        image_path = str(step.arguments["image_path"])
        validation = reactor.service.artifacts.validate_local_path(image_path)
        if validation.get("ok") is not True:
            return preflight_error(
                index,
                step,
                "image_path",
                str(validation.get("summary", "Firmware artifact is invalid.")),
                {"validation": validation},
            )
        if require_elf and Path(image_path).suffix.lower() != ".elf":
            return preflight_error(index, step, "image_path", "Debug sessions require an ELF artifact with debug symbols.")
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
ACTION_SCHEMAS = {action: schema for device_class in STEP_DEVICE_CLASSES for action, schema in device_class.step_actions.items()}
# `device` is format v3's universal routing key and is read for every kind; the
# rest are the v2 spellings, which stay valid as aliases.
ROUTE_FIELDS: tuple[str, ...] = ("device", *dict.fromkeys(device_class.route_field for device_class in STEP_DEVICE_CLASSES))
# An action can be served by more than one kind — `delay` is declared once on the
# base and therefore inherited by all of them — so this maps to every claimant
# and the name the step gave settles which one runs it.
STEP_DEVICE_CLASSES_BY_ACTION: dict[str, tuple[type[StepDevice], ...]] = {
    action: tuple(device_class for device_class in STEP_DEVICE_CLASSES if action in device_class.step_actions) for action in ACTION_SCHEMAS
}


def step_device_classes(action: str) -> tuple[type[StepDevice], ...]:
    """Every device kind that serves an action; empty for one no kind claims."""
    return STEP_DEVICE_CLASSES_BY_ACTION.get(action, ())


def step_device_class(config: AgenticHILConfig, step: TestStep) -> type[StepDevice] | None:
    """Which device kind runs this step, or None when the step does not say.

    One claimant is the ordinary case and the action alone answers it. For an
    action every kind serves, the routing key is what decides: a v2 alias names
    its kind outright, and a `device:` is looked up in the configured entries —
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

    def __init__(self, config: AgenticHILConfig, service: AgenticHILToolService, service_factory: Callable[[AgenticHILConfig], AgenticHILToolService] | None = None):
        self.config = config
        self.service = service
        self._service_factory = service_factory
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
        built = device_class(config_id, device_class.build_device(self.config, config_id), device_class.service_for(self, config_id))
        self.devices[key] = built
        return built

    def runner(self, debugger_id: str) -> DebuggerRunner:
        """The runner for one named probe, built on first use."""
        runner = self.device_for(DebuggerRunner, debugger_id)
        assert isinstance(runner, DebuggerRunner)
        return runner

    def step_device(self, step: TestStep) -> StepDevice:
        """The device a step drives. DeviceError when the plan named something
        the config does not declare — preflight refuses that before any step
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

    def cleanup_devices(self) -> list[StepDevice]:
        """Every device this run touched, in the order their sessions close.

        Each kind declares where it falls (``cleanup_order``) — debug sessions
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
        failed_step: int | None = None
        primary_error: BaseException | None = None
        try:
            for index, step in enumerate(test_config.steps, start=1):
                try:
                    result: JsonObject = self.execute_step(step)
                except Exception as error:
                    result = exception_result(
                        "test_reactor",
                        "step_exception",
                        "Test reactor step raised an exception.",
                        error,
                    )
                completed.append({"index": index, "route": step.route, "action": step.action, "result": result})
                if result_failed(result):
                    failed_step = index
                    break
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
        ok = failed_step is None and cleanup_ok
        result: JsonObject = {
            "ok": ok,
            "tool": "test_reactor",
            "name": test_config.name,
            "test_config_path": test_config.path,
            "steps": completed,
            "cleanup": cleanup,
            "cleanup_ok": cleanup_ok,
            "cleanup_errors": cleanup_errors,
            "summary": "Test reactor sequence completed." if ok else "Test reactor sequence failed.",
        }
        propagate_result_status(result, [step["result"] for step in completed] + [item["result"] for item in cleanup])
        if failed_step is not None:
            result["failed_step"] = failed_step
            step_error_type = result_error_type(completed[-1]["result"])
            result["step_error_type"] = step_error_type
            result["error_type"] = "cleanup_failed" if not cleanup_ok else step_error_type
        elif not cleanup_ok:
            result["error_type"] = "cleanup_failed"
        if not ok:
            # A run that failed aborts, and the abort calls the recovery action.
            # After the devices are closed and after this result exists, so the
            # evidence is written before anything drives the board again, and
            # `ok` is not touched: recovering the bench does not un-fail a test.
            # Reached for a failed cleanup as well as for a failed step —
            # cleanup is where the unconfirmed-effect incidents come from, and
            # those are precisely the ones that used to sit until a person came.
            result["recovery"] = self.service.recover_after_failed_run(sorted(self.devices))
        return result

    def preflight(self, test_config: TestConfig) -> JsonObject | None:
        """Refuse a plan before its first step runs, or None.

        The walk is device-kind-blind: each step is handed to the class that
        serves its action, which answers for its own config entry, its own
        permissions and its own session. Nothing here is built — see PlanState."""
        state = PlanState()
        for index, step in enumerate(test_config.steps, start=1):
            named = step.route_keys
            if len(named) > 1:
                # `device:` and its v2 alias mean the same entry, so a step
                # carrying both is a plan that has not decided. Refused rather
                # than resolved by precedence: a step that names one board under
                # one key and another under the other must be corrected, not run.
                return preflight_error(index, step, named[1], "A test step names the device it drives once.", {"route_keys": named})
            candidates = step_device_classes(step.action)
            if not candidates:
                # Unreachable through load_test_config, whose schema pass refuses
                # an unknown action first; reachable by a caller that built a
                # TestConfig itself, and silence would be a step nobody checked.
                return preflight_error(index, step, "action", "No configured device kind serves this test reactor action.", {"allowed_values": sorted(ACTION_SCHEMAS)})
            device_class = step_device_class(self.config, step)
            if device_class is None:
                return preflight_error(
                    index,
                    step,
                    ROUTE_FIELDS[0],
                    "Every device kind serves this action, so the step's `device:` must name exactly one configured entry — this one names none, or names an entry in more than one section.",
                    {claimant.configured_field: sorted(claimant.config_entries(self.config)) for claimant in candidates},
                )
            name_error = device_class.name_refusal(self, index, step)
            if name_error is not None:
                return name_error
            contract_error = self._preflight_tool_contract(index, step, device_class)
            if contract_error is not None:
                return contract_error
            if not device_class.step_action_specs[step.action].touches_device:
                # An action that drives nothing has no session to be in, no
                # permission to hold and nothing to leave behind, so the kind's
                # own refusals are about a step this one is not.
                continue
            refusal = device_class.preflight(self, index, step, state)
            if refusal is not None:
                return refusal
        return None

    def step_debugger_id(self, step: TestStep) -> str | None:
        return step_debugger_id(self.config, step)

    def _preflight_tool_contract(self, index: int, step: TestStep, device_class: type[StepDevice]) -> JsonObject | None:
        # Validate each step's plan-supplied tool arguments against the exact MCP
        # contract validators, so a step the reactor schema accepted but the tool
        # contract rejects (e.g. a traversal path) fails before any backend call
        # builds hardware or process state. Which calls a step makes is the
        # device class's own answer (`tool_calls`).
        for tool, arguments in device_class.tool_calls(self.config, step):
            error = validate_tool_arguments(tool, arguments)
            if error is not None:
                return preflight_error(index, step, error.get("field", "arguments"), f"Step arguments are rejected by the {tool} contract: {error.get('summary', 'invalid argument')}", {"tool": tool, "validation": error})
        return None


def step_debugger_id(config: AgenticHILConfig, step: TestStep) -> str | None:
    """The probe a debugger step runs on: the name the plan gave, or the only
    configured one. None means the plan must name it — with several probes
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

    Two steps naming one physical unit — a probe and its virtual COM port under
    one resource_id, the same port under two config entries — yield one device,
    because DeviceSet keys on the hardware and not on what the plan called it.

    Each step's own device class says which unit it drives (`identify`), so a
    device kind cannot be served by the reactor and left out of what the run
    locks: a plan that can drive a bus declares that bus."""
    devices: list[Device] = []
    for step in test_config.steps:
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


def preflight_error(index: int, step: TestStep, field: str, summary: str, details: JsonObject | None = None) -> JsonObject:
    return {
        "step": index,
        "field": f"steps[{index - 1}].{field}",
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
