"""The tool-definition score gate, held to the TDQS specification v1.3.

`tools/tool_definition_score.py` keeps the mean Tool Definition Quality Score
of the server's tools from falling: a change passes when the mean TDQS after it
is at least the mean TDQS before it, compared on the number, not the letter
tier. The overall score, description quality, coherence and the minimum are
reported as information and never decide. Everything in this file runs without a model and without the network.
The scorer is a fake that hands back canned answers, the command-line backend
runs a stand-in program in place of the real one, and the prompt texts are
stand-ins, because the upstream specification carries no license and its
prompts are fetched at run time and never committed.

The section names in the comments are those of the specification README at the
pinned upstream commit b9881b0cfec8: Stage 1 (context signals, invocation
cost), Stage 2 (hard gates), the LLM output contract, Stage 4 (post-processing),
Computing the score, Tiers, Flags and smells, Server-level scores, Shadowed
tools, Overall, Output format, Running TDQS at scale (the four referential
checks on shadowing risks), and the two appendices. The export tests start the
real server over stdio from this checkout, the way a host does.

The test that calls the real model lives in test_tool_definition_score_model.py.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

import pytest
import yaml
from conftest import write_authoritative_config, write_config
from support import scaled_time_bound

from agentic_hil import __version__
from agentic_hil.config import load_config
from agentic_hil.contracts import MCP_TOOL_NAMES
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TDQS_DIRECTORY = ROOT / "tools" / "tdqs"
CALIBRATION_DOCUMENT = ROOT / "docs" / "tool-definition-score.md"

sys.path.insert(0, str(ROOT / "tools"))

import tool_definition_score as tds  # noqa: E402

# Computing the score: the six dimensions in their published order, with their
# integer weights.
DIMENSIONS = (
    "purpose_clarity",
    "usage_guidelines",
    "behavioral_transparency",
    "parameter_semantics",
    "conciseness_structure",
    "contextual_completeness",
)
PUBLISHED_WEIGHTS = {
    "purpose_clarity": 25,
    "usage_guidelines": 20,
    "behavioral_transparency": 20,
    "parameter_semantics": 15,
    "conciseness_structure": 10,
    "contextual_completeness": 10,
}
# Appendix B output contract: the four coherence dimensions, as the model names them.
COHERENCE_DIMENSIONS = ("disambiguation", "naming_consistency", "tool_count_appropriateness", "completeness")
PROMPT_KEYS = {"tool_system", "tool_user", "coherence_system", "coherence_user"}
# The confirmation procedure of the brief, as the version record states it.
CONFIRMATION_PROCEDURE = {"version": 1, "initial_pairs": 1, "confirmation_pairs_on_drop": 2, "statistic": "median_per_side"}
PINNED_MODEL = "claude-opus-5-5"
PINNED_CLI_VERSION = "2.1.288"
TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
MISSING_TOKEN_LINE = f"INVALID: The tool definitions changed and {TOKEN_VARIABLE} is not set, so they cannot be scored."
GIT_CALL_S = 60
CLI_CALL_S = 60


# --- builders ----------------------------------------------------------------


def scalars(*names: str, required: list[str] | None = None) -> dict:
    """An object schema of string properties, every one required unless `required` names the subset."""
    return {
        "type": "object",
        "properties": {name: {"type": "string"} for name in names},
        "required": list(names) if required is None else required,
    }


def tool(name: str, description: object = "Does one thing.", schema: dict | None = None, **extra: object) -> dict:
    """A tool definition in the shape `tools/list` returns, with a property-less schema by default."""
    definition: dict = {"name": name, "description": description, "inputSchema": schema if schema is not None else {"type": "object", "properties": {}}}
    definition.update(extra)
    return definition


def scores(*values: int) -> dict[str, int]:
    return dict(zip(DIMENSIONS, values, strict=True))


def tool_answer(values: tuple[int, ...], *, contradiction: bool = False, summary: str = "A summary.", marker: str = "") -> str:
    """A model answer in the Appendix A output shape: every dimension with a score and a justification."""
    return json.dumps(
        {
            "scores": {
                dimension: {"score": value, "justification": f"{dimension} justification {marker}".strip()}
                for dimension, value in zip(DIMENSIONS, values, strict=True)
            },
            "annotation_contradiction": contradiction,
            "summary": summary,
        }
    )


def coherence_answer(values: tuple[int, int, int, int], *, risks: list[dict] | tuple = (), marker: str = "") -> str:
    """A model answer in the Appendix B output shape."""
    return json.dumps(
        {
            "scores": {
                dimension: {"score": value, "justification": f"{dimension} justification {marker}".strip()}
                for dimension, value in zip(COHERENCE_DIMENSIONS, values, strict=True)
            },
            "shadowing_risks": [dict(risk) for risk in risks],
            "summary": "A coherence summary.",
        }
    )


class FakeScorer:
    """The scorer interface the gate calls, answering from a table instead of a model.

    `table` maps a tool's description to the answer for it, and `default`
    answers every description the table does not name. `coherence` is the
    answer for every set, or a callable that receives the tool list. Calls are
    recorded under a lock because the gate may score concurrently."""

    def __init__(self, table: dict | None = None, coherence: object = None, default: str | None = None) -> None:
        self.table = table or {}
        self.default = default
        self.coherence = coherence if coherence is not None else coherence_answer((4, 4, 4, 4))
        self.lock = threading.Lock()
        self.tool_calls: list[tuple[str, tuple[str, ...]]] = []
        self.coherence_calls: list[tuple[str, tuple[str, ...], list]] = []

    def tool_answer(self, definition: dict, sibling_names: object) -> str:
        with self.lock:
            self.tool_calls.append((definition["name"], tuple(sibling_names)))  # type: ignore[arg-type]
        description = definition.get("description")
        if description in self.table:
            return self.table[description]
        if self.default is not None:
            return self.default
        raise KeyError(description)

    def coherence_answer(self, server_name: str, tools: list, candidates: object) -> str:
        with self.lock:
            self.coherence_calls.append((server_name, tuple(item["name"] for item in tools), list(candidates)))  # type: ignore[call-overload]
        return self.coherence(tools) if callable(self.coherence) else self.coherence  # type: ignore[return-value]


class SequenceScorer:
    """Hands back the given answers in order, repeating the last one; an exception in the list is raised."""

    def __init__(self, tool_answers: list | None = None, coherence_answers: list | None = None) -> None:
        self.tool_answers = list(tool_answers or [tool_answer((4,) * 6)])
        self.coherence_answers = list(coherence_answers or [coherence_answer((4, 4, 4, 4))])
        self.tool_calls = 0
        self.coherence_calls = 0

    @staticmethod
    def _next(answers: list) -> str:
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def tool_answer(self, definition: dict, sibling_names: object) -> str:
        self.tool_calls += 1
        return self._next(self.tool_answers)

    def coherence_answer(self, server_name: str, tools: list, candidates: object) -> str:
        self.coherence_calls += 1
        return self._next(self.coherence_answers)


class QueueScorer:
    """Scores each tool from a queue of values kept per description, one value per call.

    A value v answers v on all six dimensions, so a one-tool set scores TDQS v.
    What is left in a queue afterwards is what the gate never asked for."""

    def __init__(self, queues: dict[str, list[int]]) -> None:
        self.queues = {description: list(values) for description, values in queues.items()}
        self.lock = threading.Lock()

    def tool_answer(self, definition: dict, sibling_names: object) -> str:
        with self.lock:
            value = self.queues[definition["description"]].pop(0)
        return tool_answer((value,) * 6)

    def coherence_answer(self, server_name: str, tools: list, candidates: object) -> str:
        return coherence_answer((4, 4, 4, 4))


def standin_record(**changes: object) -> dict:
    """A version record with every field the committed one carries, and stand-in values."""
    record = {
        "model": "stand-in-model",
        "cli_version": "0.0.0",
        "spec_commit": "0123456789abcdef0123456789abcdef01234567",
        "spec_version": "1.3",
        "prompt_sha256": {key: hashlib.sha256(key.encode()).hexdigest() for key in sorted(PROMPT_KEYS)},
        "dimension_weights": dict(PUBLISHED_WEIGHTS),
        "description_quality_weights": {"mean": 60, "minimum": 40},
        "overall_weights": {"description_quality": 70, "coherence": 30},
        "tier_thresholds": {"A": 3.5, "B": 3.0, "C": 2.0, "D": 1.0},
        "retries": 2,
        "confirmation": dict(CONFIRMATION_PROCEDURE),
    }
    record.update(changes)
    return record


def export(tools: list[dict], version: str = "1.0") -> dict:
    return tds.export_from_tools(copy.deepcopy(tools), server_name="agentic-hil", server_version=version)


def mean_pairs(report: dict) -> list[tuple[Fraction, Fraction]]:
    return [(pair["base"]["rollups"]["meanTdqs"], pair["head"]["rollups"]["meanTdqs"]) for pair in report["pairs"]]


# --- round1 and the tiers (Computing the score, Tiers) ------------------------


@pytest.mark.parametrize(
    ("p", "q", "expected"),
    [
        pytest.param(285, 100, "2.9", id="the-worked-example"),
        pytest.param(25, 100, "0.3", id="tie-goes-up"),
        pytest.param(35, 100, "0.4", id="tie-goes-up-where-bankers-rounding-also-goes-up"),
        pytest.param(345, 100, "3.5", id="tie-on-the-a-boundary"),
        pytest.param(13, 4, "3.3", id="coherence-sum-13-python-round-says-3.2"),
        pytest.param(17, 4, "4.3", id="coherence-sum-17-python-round-says-4.2"),
        pytest.param(5, 4, "1.3", id="coherence-sum-5-python-round-says-1.2"),
        pytest.param(1, 3, "0.3", id="below-the-tie"),
        pytest.param(2, 3, "0.7", id="above-the-tie"),
        pytest.param(50, 10, "5.0", id="already-one-decimal"),
    ],
)
def test_round1_is_half_up_on_the_exact_rational(p: int, q: int, expected: str) -> None:
    """round1(p, q) = floor((20p + q) / (2q)) / 10, returned as an exact rational."""
    result = tds.round1(p, q)

    assert isinstance(result, Fraction)
    assert result == Fraction(expected)


def test_round1_refuses_a_float() -> None:
    """round1 never sees a float: the whole point is that no score is accumulated in doubles."""
    with pytest.raises(TypeError):
        tds.round1(2.85, 1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("score", "letter"),
    [("5.0", "A"), ("3.5", "A"), ("3.4", "B"), ("3.0", "B"), ("2.9", "C"), ("2.0", "C"), ("1.9", "D"), ("1.0", "D"), ("0.9", "F")],
)
def test_tier_boundaries(score: str, letter: str) -> None:
    assert tds.tier(Fraction(score)) == letter


# --- computeTdqs (Computing the score) ----------------------------------------


def test_the_dimensions_and_weights_are_the_published_ones() -> None:
    assert tuple(tds.DIMENSIONS) == DIMENSIONS
    assert dict(tds.DIMENSION_WEIGHTS) == PUBLISHED_WEIGHTS
    assert sum(PUBLISHED_WEIGHTS.values()) == 100


@pytest.mark.parametrize(
    ("values", "expected", "letter"),
    [
        pytest.param((4, 2, 2, 3, 4, 2), "2.9", "C", id="the-worked-example-285-hundredths"),
        pytest.param((4, 3, 3, 4, 4, 4), "3.6", "A", id="360-hundredths"),
        pytest.param((5, 4, 4, 4, 4, 4), "4.3", "A", id="425-hundredths-tie"),
        pytest.param((3, 2, 2, 2, 3, 3), "2.5", "C", id="245-hundredths-tie"),
        pytest.param((5, 5, 5, 5, 5, 5), "5.0", "A", id="ceiling"),
        pytest.param((1, 1, 1, 1, 1, 1), "1.0", "D", id="floor"),
    ],
)
def test_compute_tdqs(values: tuple[int, ...], expected: str, letter: str) -> None:
    tdqs = tds.compute_tdqs(scores(*values))

    assert tdqs == Fraction(expected)
    assert tds.tier(tdqs) == letter


def test_compute_tdqs_lands_in_a_where_float_weights_land_in_b() -> None:
    """(2, 2, 5, 5, 4, 4) is 345 hundredths exactly. Accumulated in doubles it
    lands just below the tie, which rounds to 3.4 and publishes tier B."""
    values = (2, 2, 5, 5, 4, 4)
    accumulated = 0.0
    for value, weight in zip(values, (0.25, 0.20, 0.20, 0.15, 0.10, 0.10), strict=True):
        accumulated += value * weight
    assert accumulated < 3.45

    tdqs = tds.compute_tdqs(scores(*values))

    assert tdqs == Fraction("3.5")
    assert tds.tier(tdqs) == "A"


# --- server rollups (Server-level scores, Overall) ----------------------------


def test_description_quality_uses_the_exact_mean_not_the_published_one() -> None:
    """TDQS 2.0, 2.0, 3.7: the exact mean 77/30 gives 0.6 x 2.567 + 0.4 x 2.0 = 2.34, so 2.3.
    Rounding the mean to the published 2.6 first would give 2.36, so 2.4."""
    assert tds.description_quality([Fraction("2.0"), Fraction("2.0"), Fraction("3.7")]) == Fraction("2.3")


def test_description_quality_rounds_an_exact_tie_up() -> None:
    """TDQS 2.6, 2.9, 2.9, 5.0: mean 3.35, minimum 2.6, so exactly 3.05, which is 3.1."""
    assert tds.description_quality([Fraction("2.6"), Fraction("2.9"), Fraction("2.9"), Fraction("5.0")]) == Fraction("3.1")


@pytest.mark.parametrize(
    ("values", "expected"),
    [((4, 3, 3, 3), "3.3"), ((5, 4, 4, 4), "4.3"), ((2, 1, 1, 1), "1.3"), ((4, 4, 4, 4), "4.0"), ((5, 5, 5, 4), "4.8")],
)
def test_coherence_is_the_half_up_mean_of_its_four_dimensions(values: tuple[int, int, int, int], expected: str) -> None:
    assert tuple(tds.COHERENCE_DIMENSIONS) == COHERENCE_DIMENSIONS
    assert tds.coherence_score(dict(zip(COHERENCE_DIMENSIONS, values, strict=True))) == Fraction(expected)


def test_overall_lands_in_a_where_floats_publish_b() -> None:
    """The specification's own case: 0.7 x 3.0 + 0.3 x 4.5 is 3.45 exactly, just under it in doubles."""
    assert 0.7 * 3.0 + 0.3 * 4.5 < 3.45

    overall = tds.overall_score(Fraction("3.0"), Fraction("4.5"))

    assert overall == Fraction("3.5")
    assert tds.tier(overall) == "A"


def test_overall_of_the_published_server_example() -> None:
    """Output format, per server: description quality 2.9 and coherence 4.5 give 3.4, tier B."""
    overall = tds.overall_score(Fraction("2.9"), Fraction("4.5"))

    assert overall == Fraction("3.4")
    assert tds.tier(overall) == "B"


def test_rollups_report_counts_the_minimum_tool_and_every_score() -> None:
    """TDQS 2.6, 2.9, 2.9, 5.0: the published mean is round1(13.4, 4), so 3.4."""
    results = {"a": {"tdqs": Fraction("2.6")}, "b": {"tdqs": Fraction("2.9")}, "c": {"tdqs": Fraction("2.9")}, "d": {"tdqs": Fraction("5.0")}}

    rollups = tds.rollups(results, ["a", "b", "c", "d"], {"coherenceScore": Fraction("3.3")})

    assert rollups["toolCount"] == 4
    assert rollups["scoredToolCount"] == 4
    assert rollups["meanTdqs"] == Fraction("3.4")
    assert rollups["minTdqs"] == Fraction("2.6")
    assert rollups["minTool"] == "a"
    assert rollups["descriptionQualityScore"] == Fraction("3.1")
    assert rollups["descriptionQualityTier"] == "B"
    assert rollups["coherenceScore"] == Fraction("3.3")
    assert rollups["coherenceTier"] == "B"
    assert rollups["overallScore"] == Fraction("3.2")
    assert rollups["overallTier"] == "B"


@pytest.mark.parametrize(("scored", "total"), [(2, 3), (4, 5), (43, 44)], ids=["2-of-3", "4-of-5", "43-of-44"])
def test_rollups_refuse_an_unscored_tool(scored: int, total: int) -> None:
    """The registry rolls up at 80 % coverage; the gate needs every tool scored.
    4 of 5 is exactly the registry's threshold and 43 of 44 is above it."""
    assert issubclass(tds.IncompleteResult, tds.InvalidComparison)
    names = [f"tool_{index:02d}" for index in range(total)]
    results = {name: {"tdqs": Fraction("4.0")} for name in names[:scored]}

    with pytest.raises(tds.IncompleteResult, match=names[-1]):
        tds.rollups(results, names, {"coherenceScore": Fraction("4.0")})


# --- context signals (Stage 1) ------------------------------------------------


def test_context_signals_of_the_published_example() -> None:
    """Output format, per tool: four parameters, one required, two described: coverage 50, cost 1."""
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Record id."},
            "name": {"type": "string", "description": "New name."},
            "tags": {"type": "array", "items": {"type": "string"}},
            "note": {"type": "string", "description": ""},
        },
        "required": ["id"],
    }

    signals = tds.context_signals(tool("update_record", "Update a record.", schema))

    assert {key: signals[key] for key in (
        "paramCount", "requiredParamCount", "paramsWithDescriptions", "paramsWithEnums", "schemaDescriptionCoverage",
        "hasNestedObjects", "requiredFieldCount", "schemaDepth", "unionChoiceCount", "invocationCost",
        "hasOutputSchema", "hasAnnotations", "titleIsMeaningful",
    )} == {
        "paramCount": 4,
        "requiredParamCount": 1,
        "paramsWithDescriptions": 2,
        "paramsWithEnums": 0,
        "schemaDescriptionCoverage": 50,
        "hasNestedObjects": False,
        "requiredFieldCount": 1,
        "schemaDepth": 1,
        "unionChoiceCount": 0,
        "invocationCost": 1,
        "hasOutputSchema": False,
        "hasAnnotations": False,
        "titleIsMeaningful": False,
    }
    assert signals["annotationValues"] == {"readOnly": None, "destructive": None, "idempotent": None, "openWorld": None}


@pytest.mark.parametrize(("described", "total", "coverage"), [(0, 0, 100), (2, 3, 67), (1, 3, 33), (3, 3, 100), (0, 2, 0)])
def test_schema_description_coverage(described: int, total: int, coverage: int) -> None:
    """round(described / total x 100), and 100 for a tool with no parameters."""
    properties = {f"p{index}": {"type": "string", **({"description": f"Parameter {index}."} if index < described else {})} for index in range(total)}

    signals = tds.context_signals(tool("t", schema={"type": "object", "properties": properties}))

    assert signals["paramCount"] == total
    assert signals["paramsWithDescriptions"] == described
    assert signals["schemaDescriptionCoverage"] == coverage


def test_enums_and_nested_objects_are_counted() -> None:
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["fast", "slow"]},
            "level": {"enum": [1, 2, 3]},
            "options": {"type": "object", "properties": {"x": {"type": "string"}}},
        },
        "required": ["mode"],
    }

    signals = tds.context_signals(tool("t", schema=schema))

    assert signals["paramsWithEnums"] == 2
    assert signals["hasNestedObjects"] is True
    assert tds.context_signals(tool("t", schema=scalars("a", "b")))["hasNestedObjects"] is False


ANNOTATION_HINTS = {"readOnlyHint": "readOnly", "destructiveHint": "destructive", "idempotentHint": "idempotent", "openWorldHint": "openWorld"}


@pytest.mark.parametrize("hint", sorted(ANNOTATION_HINTS))
@pytest.mark.parametrize("value", [True, False, "absent", "yes"], ids=["true", "false", "absent", "not-a-boolean"])
def test_each_annotation_hint_is_read_on_its_own(hint: str, value: object) -> None:
    """Each hint reads as declared true, declared false, or undeclared (None); a value
    that is not a boolean declares nothing. The other three stay undeclared."""
    annotations = {"title": "A title"} if value == "absent" else {"title": "A title", hint: value}

    signals = tds.context_signals(tool("t", annotations=annotations))

    expected = dict.fromkeys(ANNOTATION_HINTS.values())
    if isinstance(value, bool):
        expected[ANNOTATION_HINTS[hint]] = value
    assert signals["annotationValues"] == expected
    assert signals["hasAnnotations"] is True


def test_empty_or_absent_annotations_are_no_annotations() -> None:
    assert tds.context_signals(tool("t", annotations={}))["hasAnnotations"] is False
    assert tds.context_signals(tool("t"))["hasAnnotations"] is False


@pytest.mark.parametrize(
    ("output_schema", "present"),
    [("absent", False), ({}, False), ({"type": "object", "properties": {"ok": {"type": "boolean"}}}, True)],
)
def test_has_output_schema(output_schema: object, present: bool) -> None:
    definition = tool("t") if output_schema == "absent" else tool("t", outputSchema=output_schema)

    assert tds.context_signals(definition)["hasOutputSchema"] is present


@pytest.mark.parametrize(
    ("title", "meaningful"),
    [(None, False), ("run_tests", False), ("Run", False), ("Run the test suite", True)],
)
def test_title_is_meaningful_when_it_exists_differs_and_is_longer(title: str | None, meaningful: bool) -> None:
    definition = tool("run_tests") if title is None else tool("run_tests", title=title)

    assert tds.context_signals(definition)["titleIsMeaningful"] is meaningful


def test_the_title_is_the_top_level_one_not_the_annotation_title() -> None:
    """What gets scored names `title` as the optional MCP display title, a field of the
    definition; the annotation title is part of the annotations block."""
    definition = tool("run_tests", annotations={"title": "Run the test suite"})

    assert tds.context_signals(definition)["titleIsMeaningful"] is False


def test_definition_bytes_and_input_hash_follow_the_canonical_serialization() -> None:
    definition = tool("read_thing", "Read a thing.", scalars("id"), title="Read a thing", annotations={"readOnlyHint": True})

    canonical = tds.canonical_definition(definition)
    signals = tds.context_signals(definition)

    assert isinstance(canonical, bytes)
    assert signals["definitionBytes"] == len(canonical)
    assert signals["inputHash"] == hashlib.sha256(canonical).hexdigest()[:16]
    assert tds.definition_hash(definition) == hashlib.sha256(canonical).hexdigest()


# --- invocation cost (Stage 1, Invocation cost) -------------------------------

FOUR_FLAT = scalars("player", "season", "stat", "team")
EIGHT_NAMES = [f"field_{index}" for index in range(8)]
EIGHT_FLAT = scalars(*EIGHT_NAMES)
EIGHT_WRAPPED = {"type": "object", "properties": {"args": scalars(*EIGHT_NAMES)}, "required": ["args"]}

# One required object that requires three fields, one of them a nested object
# holding a three-branch discriminated union whose widest branch requires one
# field: 5 required fields over a subtree of depth 3, so 5 + 2 x 2 + 2 x 2 = 13.
QUERY_PANEL = {
    "type": "object",
    "properties": {
        "query": {
            "type": "object",
            "properties": {
                "population": {"type": "string"},
                "window": {"type": "string"},
                "measure": {
                    "type": "object",
                    "oneOf": [
                        {"type": "object", "properties": {"kind": {"const": "count"}}, "required": ["kind"]},
                        {"type": "object", "properties": {"kind": {"const": "rate"}, "per": {"type": "integer"}}, "required": ["kind"]},
                        {"type": "object", "properties": {"kind": {"const": "share"}, "of": {"type": "string"}}, "required": ["kind"]},
                    ],
                },
            },
            "required": ["population", "window", "measure"],
        },
        "format": {"type": "string"},
    },
    "required": ["query"],
}

WIDEST_BRANCH = {
    "type": "object",
    "properties": {
        "target": {
            "type": "object",
            "oneOf": [
                {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
                scalars("a", "b", "c"),
                {"type": "object", "properties": {"b": {"type": "string"}}, "required": ["b"]},
            ],
        }
    },
    "required": ["target"],
}

TOP_LEVEL_UNION = {
    "type": "object",
    "properties": {"port": {"type": "string"}},
    "required": ["port"],
    "oneOf": [scalars("text"), scalars("hex")],
}

SCALAR_UNION = {"type": "object", "properties": {"frame_id": {"anyOf": [{"type": "integer"}, {"type": "string"}]}}, "required": ["frame_id"]}
NULLABLE = {"type": "object", "properties": {"label": {"anyOf": [{"type": "string"}, {"type": "null"}]}}, "required": ["label"]}
ENUM_AND_TYPE_ARRAY = {
    "type": "object",
    "properties": {"mode": {"enum": ["a", "b", "c"]}, "value": {"type": ["string", "integer", "boolean"]}},
    "required": ["mode", "value"],
}
OPTIONAL_DEPTH = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "filter": {"type": "object", "properties": {"range": scalars("lo", "hi")}, "required": ["range"]},
        "choice": {"oneOf": [{"type": "string"}, {"type": "integer"}, {"type": "boolean"}]},
    },
    "required": ["id"],
}
ARRAY_OF_OBJECTS = {
    "type": "object",
    "properties": {"changes": {"type": "array", "items": scalars("key", "note", required=["key"])}},
    "required": ["changes"],
}
REF_DEFS = {"type": "object", "properties": {"cfg": {"$ref": "#/$defs/Cfg"}}, "required": ["cfg"], "$defs": {"Cfg": scalars("a", "b")}}
REF_DEFINITIONS = {"type": "object", "properties": {"cfg": {"$ref": "#/definitions/Cfg"}}, "required": ["cfg"], "definitions": {"Cfg": scalars("a", "b")}}
ONLY_OPTIONAL = {"type": "object", "properties": {"verbose": {"type": "boolean"}}}

# allOf: every branch must hold, so their required fields add up; depth is the deepest.
# cfg (1) + a, b (2) + c (1) = 4 over depth 2: 4 + 2 = 6.
ALL_OF = {"type": "object", "properties": {"cfg": {"allOf": [scalars("a", "b"), scalars("c")]}}, "required": ["cfg"]}
# `not` is traversed for depth and nothing else: x (1) over depth 3: 1 + 2 x 2 = 5.
NOT_DEPTH = {
    "type": "object",
    "properties": {
        "x": {
            "type": "object",
            "properties": {"y": {"type": "string"}},
            "not": {"type": "object", "properties": {"z": scalars("w")}, "required": ["z"]},
        }
    },
    "required": ["x"],
}
# The widest branch (three fields at depth 1) is not the deepest (two fields at depth 2):
# the count comes from the widest and the depth from the deepest, 1 + 3 = 4 over depth 3
# with one union choice: 4 + 2 x 2 + 2 x 1 = 10.
UNEQUAL_UNION = {
    "type": "object",
    "properties": {
        "target": {
            "oneOf": [
                scalars("a", "b", "c"),
                {"type": "object", "properties": {"inner": scalars("x")}, "required": ["inner"]},
            ]
        }
    },
    "required": ["target"],
}
# One definition required twice is constructed twice: a and b (2) + x, y twice (4) = 6
# over depth 2: 6 + 2 = 8. Only a pointer already on the path from the root is a repeat.
SHARED_DEFINITION = {
    "type": "object",
    "properties": {"a": {"$ref": "#/$defs/Pair"}, "b": {"$ref": "#/$defs/Pair"}},
    "required": ["a", "b"],
    "$defs": {"Pair": scalars("x", "y")},
}
# Branches that only name what they require take the properties of the object they sit
# on: port (1) + the widest branch (1) = 2 at depth 1, one union choice: 2 + 2 = 4.
REQUIRED_ONLY_BRANCHES = {
    "type": "object",
    "properties": {"port": {"type": "string"}, "text": {"type": "string"}, "hex": {"type": "string"}},
    "required": ["port"],
    "oneOf": [{"required": ["text"]}, {"required": ["hex"]}],
}
# node (1) + value (1) + next (1, a repeat of the pointer on the path, so a leaf) = 3
# over depth 2: 3 + 2 = 5.
RECURSIVE = {
    "type": "object",
    "properties": {"node": {"$ref": "#/$defs/Node"}},
    "required": ["node"],
    "$defs": {"Node": {"type": "object", "properties": {"value": {"type": "string"}, "next": {"$ref": "#/$defs/Node"}}, "required": ["value", "next"]}},
}


def cost_signals(schema: dict) -> tuple[int, int, int, int]:
    signals = tds.context_signals(tool("t", schema=schema))
    return (signals["requiredFieldCount"], signals["schemaDepth"], signals["unionChoiceCount"], signals["invocationCost"])


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        pytest.param(FOUR_FLAT, (4, 1, 0, 4), id="four-required-scalars-cost-4"),
        pytest.param(EIGHT_FLAT, (8, 1, 0, 8), id="eight-flat-cost-8"),
        pytest.param(EIGHT_WRAPPED, (9, 2, 0, 11), id="eight-wrapped-cost-11"),
        pytest.param(QUERY_PANEL, (5, 3, 2, 13), id="the-13-example"),
        pytest.param(WIDEST_BRANCH, (4, 2, 2, 10), id="only-the-widest-branch-counts"),
        pytest.param(TOP_LEVEL_UNION, (2, 1, 1, 4), id="a-union-on-the-root"),
        pytest.param(SCALAR_UNION, (1, 1, 1, 3), id="anyof-of-two-scalars"),
        pytest.param(NULLABLE, (1, 1, 0, 1), id="the-nullable-idiom-is-no-choice"),
        pytest.param(ENUM_AND_TYPE_ARRAY, (2, 1, 0, 2), id="enum-and-type-arrays-are-no-choice"),
        pytest.param(OPTIONAL_DEPTH, (1, 1, 0, 1), id="optional-nesting-and-unions-cost-nothing"),
        pytest.param(ARRAY_OF_OBJECTS, (2, 2, 0, 4), id="an-array-of-objects-adds-a-level"),
        pytest.param(REF_DEFS, (3, 2, 0, 5), id="ref-into-defs"),
        pytest.param(REF_DEFINITIONS, (3, 2, 0, 5), id="ref-into-definitions"),
        pytest.param(ONLY_OPTIONAL, (0, 1, 0, 0), id="no-required-parameter"),
        pytest.param(ALL_OF, (4, 2, 0, 6), id="allof-branches-add-up"),
        pytest.param(NOT_DEPTH, (1, 3, 0, 5), id="not-adds-depth-only"),
        pytest.param(UNEQUAL_UNION, (4, 3, 1, 10), id="widest-branch-for-count-deepest-for-depth"),
        pytest.param(SHARED_DEFINITION, (6, 2, 0, 8), id="a-definition-required-twice-counts-twice"),
        pytest.param(REQUIRED_ONLY_BRANCHES, (2, 1, 1, 4), id="required-only-branches-use-the-parents-properties"),
        pytest.param(RECURSIVE, (3, 2, 0, 5), id="a-recursive-reference-stops-at-the-repeat"),
    ],
)
def test_invocation_cost(schema: dict, expected: tuple[int, int, int, int]) -> None:
    """requiredFieldCount + 2 x max(0, schemaDepth - 1) + 2 x unionChoiceCount, over the required subtree."""
    signals = tds.context_signals(tool("t", schema=schema))

    assert cost_signals(schema) == expected
    assert signals["requiredFieldCount"] >= signals["requiredParamCount"]


@pytest.mark.parametrize("schema", ["absent", None, {}, {"type": "object"}, {"type": "object", "properties": {}}, {"type": "object", "properties": {}, "required": []}])
def test_an_absent_empty_or_property_less_schema_costs_nothing(schema: object) -> None:
    definition: dict = {"name": "ping", "description": "Check the server answers."}
    if schema != "absent":
        definition["inputSchema"] = schema

    signals = tds.context_signals(definition)

    assert (signals["requiredFieldCount"], signals["schemaDepth"], signals["unionChoiceCount"], signals["invocationCost"]) == (0, 0, 0, 0)
    assert signals["paramCount"] == 0
    assert signals["schemaDescriptionCoverage"] == 100


def test_depth_is_capped_at_ten() -> None:
    """Fifteen nested required objects: the traversal stops below the tenth level, so the
    tenth counts its own required child and nothing under it: 10 fields at depth 10."""
    schema: dict = scalars("leaf")
    for _ in range(14):
        schema = {"type": "object", "properties": {"child": schema}, "required": ["child"]}

    assert cost_signals(schema) == (10, 10, 0, 10 + 2 * 9)


# --- hashing and change detection ---------------------------------------------


def reversed_keys(value: object) -> object:
    """The same JSON value with every object's keys in reverse insertion order."""
    if isinstance(value, dict):
        return {key: reversed_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [reversed_keys(item) for item in value]
    return value


HASHED = tool(
    "read_thing",
    "Read a thing by its id.",
    scalars("id", "revision", required=["id"]),
    title="Read a thing",
    annotations={"title": "Read a thing", "readOnlyHint": True, "openWorldHint": False},
)


def test_definition_hash_is_a_full_sha256_stable_under_key_order() -> None:
    digest = tds.definition_hash(HASHED)

    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert tds.definition_hash(reversed_keys(HASHED)) == digest
    assert tds.canonical_definition(reversed_keys(HASHED)) == tds.canonical_definition(HASHED)
    assert tds.context_signals(HASHED)["inputHash"] == digest[:16]


def test_the_canonical_bytes_are_the_six_fields_as_sorted_compact_utf8_json() -> None:
    """Decoded on their own, the bytes hold exactly the six fields, an absent one as null,
    with every object's keys sorted, no whitespace between tokens and non-ASCII text
    as UTF-8 rather than escapes."""
    schema = {"type": "object", "required": ["wait"], "properties": {"wait": {"type": "integer", "description": "Wartezeit in µs."}}}
    definition = {"name": "greet", "description": "Grüße senden.", "inputSchema": schema, "annotations": {"readOnlyHint": False}, "_meta": {"x": 1}}

    canonical = tds.canonical_definition(definition)
    text = canonical.decode("utf-8")
    decoded = json.loads(text)

    assert decoded == {
        "annotations": {"readOnlyHint": False},
        "description": "Grüße senden.",
        "inputSchema": schema,
        "name": "greet",
        "outputSchema": None,
        "title": None,
    }
    assert list(decoded) == sorted(decoded)
    assert list(decoded["inputSchema"]) == sorted(decoded["inputSchema"])
    assert "Grüße".encode() in canonical
    assert "µs".encode() in canonical
    assert "\\u" not in text
    assert not re.search(r'[,:]\s', text.replace("Grüße senden.", "").replace("Wartezeit in µs.", ""))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "read_other_thing"),
        ("title", "Read another thing"),
        ("description", "Read a thing by its id, reworded."),
        ("inputSchema", scalars("key")),
        ("outputSchema", {"type": "object", "properties": {"value": {"type": "string"}}}),
        ("annotations", {"readOnlyHint": False}),
    ],
)
def test_each_definition_field_moves_the_hash(field: str, value: object) -> None:
    changed = {**copy.deepcopy(HASHED), field: value}

    assert tds.definition_hash(changed) != tds.definition_hash(HASHED)


def test_fields_outside_the_definition_do_not_move_the_hash() -> None:
    """The hash covers name, title, description, inputSchema, outputSchema and annotations only."""
    decorated = {**copy.deepcopy(HASHED), "_meta": {"origin": "test"}}

    assert tds.definition_hash(decorated) == tds.definition_hash(HASHED)


def test_set_hash_and_diff_see_every_kind_of_change() -> None:
    base = [tool("a", "A."), tool("b", "B."), tool("c", "C.")]
    added = [*base, tool("d", "D.")]
    removed = base[:2]
    changed = [base[0], tool("b", "B, reworded."), base[2]]

    assert tds.set_hash(copy.deepcopy(base)) == tds.set_hash(base)
    assert len({tds.set_hash(tools) for tools in (base, added, removed, changed)}) == 4

    diff = tds.diff_definitions(base, [tool("a", "A."), tool("b", "B, reworded."), tool("d", "D.")])

    assert list(diff.added) == ["d"]
    assert list(diff.removed) == ["c"]
    assert list(diff.changed) == ["b"]
    assert list(diff.unchanged) == ["a"]


def test_the_set_hash_does_not_depend_on_listing_order() -> None:
    tools = [tool("a", "A."), tool("b", "B.", FOUR_FLAT), tool("c", "C.")]

    assert tds.set_hash(list(reversed(tools))) == tds.set_hash(tools)
    assert tds.set_hash([tools[1], tools[0], tools[2]]) == tds.set_hash(tools)


def test_a_reordered_listing_passes_without_any_model_call() -> None:
    tools = [tool("alpha_tool", "Alpha."), tool("beta_tool", "Beta.", FOUR_FLAT), tool("gamma_tool", "Gamma.")]
    scorer = FakeScorer()

    report = tds.compare(export(tools), export(list(reversed(tools))), scorer, standin_record())

    assert report["decision"] == "pass"
    assert report["pairs"] == []
    assert scorer.tool_calls == []
    assert scorer.coherence_calls == []


def test_a_listing_with_a_duplicate_name_is_refused() -> None:
    with pytest.raises(tds.InvalidComparison, match="alpha_tool"):
        export([tool("alpha_tool", "Alpha."), tool("alpha_tool", "Alpha again.")])


def test_export_from_tools_carries_the_definitions_and_their_hashes() -> None:
    tools = [tool("a", "A."), HASHED]

    exported = tds.export_from_tools(copy.deepcopy(tools), server_name="agentic-hil", server_version="1.0")

    assert exported["serverName"] == "agentic-hil"
    assert exported["serverVersion"] == "1.0"
    assert exported["tools"] == tools
    assert exported["hashes"] == {item["name"]: tds.definition_hash(item) for item in tools}
    assert exported["setHash"] == tds.set_hash(tools)


# --- what the definitions cost (the size report, informational) ---------------

GREETING = "Grüße, µs."  # 10 characters, 13 bytes in UTF-8


def compact_utf8_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def test_the_export_measures_characters_bytes_and_each_definition() -> None:
    """Description characters count characters; the tools/list result and each
    definition count UTF-8 bytes, so non-ASCII text tells them apart."""
    tools = [tool("read_greeting", GREETING), tool("ping", "Ping.")]

    size = export(tools)["size"]

    assert size["descriptionCharacters"] == len(GREETING) + len("Ping.") == 15
    assert size["toolsListBytes"] == compact_utf8_bytes({"tools": tools})
    assert size["toolsListBytes"] > len(json.dumps({"tools": tools}, ensure_ascii=False, separators=(",", ":")))
    ping_canonical = {"annotations": None, "description": "Ping.", "inputSchema": {"properties": {}, "type": "object"}, "name": "ping", "outputSchema": None, "title": None}
    assert size["definitionBytes"]["ping"] == len(json.dumps(ping_canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert size["definitionBytes"] == {item["name"]: len(tds.canonical_definition(item)) for item in tools}


def size_row(summary: str, label: str, base: int, head: int, delta: int) -> re.Match | None:
    signed = f"+{delta}" if delta > 0 else str(delta)
    return re.search(rf"^\|\s*{re.escape(label)}\s*\|\s*{base}\s*\|\s*{head}\s*\|\s*{re.escape(signed)}\s*\|", summary, re.MULTILINE)


def test_a_bigger_head_with_equal_scores_passes_and_the_report_shows_the_cost() -> None:
    """The size report is information: a longer description, a tool removed and a
    larger one added change the numbers, never the decision."""
    base_tools = [tool("read_greeting", "Grüße."), tool("ping", "Ping."), tool("old_tool", "Old.")]
    head_tools = [tool("read_greeting", "Grüße, µs, and a good deal more text."), tool("ping", "Ping."), tool("new_tool", "New, and larger.", FOUR_FLAT)]
    scorer = FakeScorer(default=tool_answer((4,) * 6))
    base_export, head_export = export(base_tools), export(head_tools)

    report = tds.compare(base_export, head_export, scorer, standin_record())

    assert report["decision"] == "pass"
    assert len(report["pairs"]) == 1
    base_size, head_size = base_export["size"], head_export["size"]
    assert head_size["toolsListBytes"] > base_size["toolsListBytes"]
    assert report["size"]["base"] == base_size
    assert report["size"]["head"] == head_size
    delta = report["size"]["delta"]
    assert delta["descriptionCharacters"] == head_size["descriptionCharacters"] - base_size["descriptionCharacters"]
    assert delta["toolsListBytes"] == head_size["toolsListBytes"] - base_size["toolsListBytes"]
    assert delta["definitionBytes"] == {
        "read_greeting": head_size["definitionBytes"]["read_greeting"] - base_size["definitionBytes"]["read_greeting"],
        "ping": 0,
        "old_tool": -base_size["definitionBytes"]["old_tool"],
        "new_tool": head_size["definitionBytes"]["new_tool"],
    }
    summary = tds.summary_markdown(report)
    assert size_row(summary, "Description characters", base_size["descriptionCharacters"], head_size["descriptionCharacters"], delta["descriptionCharacters"])
    assert size_row(summary, "tools/list bytes", base_size["toolsListBytes"], head_size["toolsListBytes"], delta["toolsListBytes"])
    assert size_row(summary, "`new_tool`", 0, head_size["definitionBytes"]["new_tool"], delta["definitionBytes"]["new_tool"])
    assert size_row(summary, "`old_tool`", base_size["definitionBytes"]["old_tool"], 0, delta["definitionBytes"]["old_tool"])
    assert size_row(summary, "`read_greeting`", base_size["definitionBytes"]["read_greeting"], head_size["definitionBytes"]["read_greeting"], delta["definitionBytes"]["read_greeting"])
    assert json.loads(tds.report_json(report))["size"]["delta"]["toolsListBytes"] == delta["toolsListBytes"]


def test_the_size_report_is_there_when_nothing_changed() -> None:
    tools = [tool("read_greeting", GREETING)]

    report = tds.compare(export(tools), export(tools), None, standin_record())

    assert report["size"]["delta"] == {"descriptionCharacters": 0, "toolsListBytes": 0, "definitionBytes": {"read_greeting": 0}}
    assert size_row(tds.summary_markdown(report), "Description characters", len(GREETING), len(GREETING), 0)


# --- hard gates and post-processing (Stage 2, Stage 4) ------------------------


@pytest.mark.parametrize("description", ["absent", None, "", "   \n\t "])
def test_a_missing_description_is_scored_without_the_model(description: object) -> None:
    definition = tool("ghost", description)
    if description == "absent":
        del definition["description"]
    scorer = FakeScorer()

    result = tds.evaluate_tool(definition, ["other_tool"], scorer)

    assert scorer.tool_calls == []
    assert result["scores"] == dict.fromkeys(DIMENSIONS, 1)
    assert set(result["justifications"]) == set(DIMENSIONS)
    for dimension in DIMENSIONS:
        assert result["justifications"][dimension]["score"] == 1
        assert isinstance(result["justifications"][dimension]["justification"], str)
        assert result["justifications"][dimension]["justification"].strip()
    assert result["smells"] == list(DIMENSIONS)
    assert result["tdqs"] == Fraction("1.0")
    assert result["tier"] == "D"
    assert result["flags"] == ["No Description"]


def test_a_tautological_description_caps_purpose_clarity_at_two() -> None:
    """The description lowercased and trimmed is the name. The model is still asked; its 5 becomes 2."""
    scorer = FakeScorer({"  Process\n": tool_answer((5, 5, 5, 5, 5, 5))})

    result = tds.evaluate_tool(tool("process", "  Process\n"), [], scorer)

    assert len(scorer.tool_calls) == 1
    assert result["scores"]["purpose_clarity"] == 2
    assert result["justifications"]["purpose_clarity"]["score"] == 2
    assert result["tdqs"] == Fraction("4.3")
    assert result["flags"] == ["Tautological Description"]
    assert result["smells"] == ["purpose_clarity"]


def test_the_tautology_cap_never_raises_a_lower_score() -> None:
    scorer = FakeScorer({"process": tool_answer((1, 5, 5, 5, 5, 5))})

    result = tds.evaluate_tool(tool("process", "process"), [], scorer)

    assert result["scores"]["purpose_clarity"] == 1
    assert result["tdqs"] == Fraction("4.0")


def test_a_description_that_is_the_title_is_tautological() -> None:
    scorer = FakeScorer({"  Run Job ": tool_answer((5, 5, 5, 5, 5, 5))})

    result = tds.evaluate_tool(tool("run_job", "  Run Job ", title="run job"), [], scorer)

    assert result["flags"] == ["Tautological Description"]
    assert result["scores"]["purpose_clarity"] == 2


def test_a_description_that_only_starts_with_the_name_is_not_tautological() -> None:
    scorer = FakeScorer({"Process the queue.": tool_answer((5, 5, 5, 5, 5, 5))})

    result = tds.evaluate_tool(tool("process", "Process the queue."), [], scorer)

    assert result["flags"] == []
    assert result["scores"]["purpose_clarity"] == 5
    assert result["tdqs"] == Fraction("5.0")


def test_post_processing_flags_a_contradiction_and_lists_smells_in_dimension_order() -> None:
    """The published contradiction example: transparency 1, so 265 hundredths, 2.7, tier C."""
    definition = tool("create_record", "Creates a new record.", annotations={"readOnlyHint": True})
    scorer = FakeScorer({"Creates a new record.": tool_answer((4, 2, 1, 3, 4, 2), contradiction=True)})

    result = tds.evaluate_tool(definition, ["get_record"], scorer)

    assert result["flags"] == ["Annotation Contradiction"]
    assert result["smells"] == ["usage_guidelines", "behavioral_transparency", "contextual_completeness"]
    assert result["scores"] == scores(4, 2, 1, 3, 4, 2)
    assert result["tdqs"] == Fraction("2.7")
    assert result["tier"] == "C"
    assert result["justifications"]["usage_guidelines"] == {"score": 2, "justification": "usage_guidelines justification"}
    assert set(result["justifications"]) == set(DIMENSIONS)
    assert result["summary"] == "A summary."
    assert result["serverFlags"] == []
    assert result["definitionHash"] == tds.definition_hash(definition)
    assert result["contextSignals"] == tds.context_signals(definition)


def test_the_flags_keep_their_published_order() -> None:
    scorer = FakeScorer({"process": tool_answer((5, 5, 5, 5, 5, 5), contradiction=True)})

    result = tds.evaluate_tool(tool("process", "process", annotations={"readOnlyHint": True}), [], scorer)

    assert result["flags"] == ["Tautological Description", "Annotation Contradiction"]


# --- the output contract and the retry limit (LLM output contract) ------------

GOOD_TOOL_ANSWER = json.loads(tool_answer((4, 2, 2, 3, 4, 2)))
GOOD_COHERENCE_ANSWER = json.loads(coherence_answer((4, 3, 3, 3)))


def changed_answer(answer: dict, change: Callable[[dict], object]) -> str:
    edited = copy.deepcopy(answer)
    change(edited)
    return json.dumps(edited)


def set_score(value: object, dimension: str = "purpose_clarity") -> Callable[[dict], object]:
    return lambda answer: answer["scores"][dimension].__setitem__("score", value)


def drop_justification(dimension: str) -> Callable[[dict], object]:
    return lambda answer: answer["scores"][dimension].pop("justification")


def set_justification(dimension: str, value: object) -> Callable[[dict], object]:
    return lambda answer: answer["scores"][dimension].__setitem__("justification", value)


def drop_dimension(dimension: str) -> Callable[[dict], object]:
    return lambda answer: answer["scores"].pop(dimension)


def set_field(field: str, value: object) -> Callable[[dict], object]:
    return lambda answer: answer.__setitem__(field, value)


def drop_field(field: str) -> Callable[[dict], object]:
    return lambda answer: answer.pop(field)


BAD_TOOL_ANSWERS = [
    pytest.param("this is not json", id="not-json"),
    pytest.param("[]", id="a-list"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, set_field("scores", [])), id="scores-not-an-object"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, drop_dimension("parameter_semantics")), id="a-dimension-missing"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, lambda a: a["scores"].__setitem__("purpose_clarity", 4)), id="a-dimension-not-an-object"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, set_score(0)), id="score-0"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, set_score(6)), id="score-6"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, set_score(3.5)), id="score-3.5"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, set_score("4")), id="score-a-string"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, set_score(True)), id="score-a-boolean"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, drop_justification("usage_guidelines")), id="justification-missing"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, set_justification("usage_guidelines", 5)), id="justification-not-a-string"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, drop_field("annotation_contradiction")), id="contradiction-missing"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, set_field("annotation_contradiction", "false")), id="contradiction-a-string"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, drop_field("summary")), id="summary-missing"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, set_field("summary", 3)), id="summary-not-a-string"),
]


def test_a_good_tool_answer_parses() -> None:
    parsed = tds.parse_tool_answer(json.dumps(GOOD_TOOL_ANSWER))

    assert {dimension: parsed["scores"][dimension]["score"] for dimension in DIMENSIONS} == scores(4, 2, 2, 3, 4, 2)
    assert parsed["annotation_contradiction"] is False


def test_an_answer_in_a_json_fence_parses() -> None:
    parsed = tds.parse_tool_answer("```json\n" + json.dumps(GOOD_TOOL_ANSWER) + "\n```")

    assert parsed["scores"]["purpose_clarity"]["score"] == 4
    assert tds.parse_coherence_answer("```json\n" + json.dumps(GOOD_COHERENCE_ANSWER) + "\n```")["scores"]["disambiguation"]["score"] == 4


@pytest.mark.parametrize("text", BAD_TOOL_ANSWERS)
def test_a_bad_tool_answer_is_refused(text: str) -> None:
    with pytest.raises(tds.InvalidAnswer):
        tds.parse_tool_answer(text)


def risk(**fields: object) -> dict:
    return {"tool": "a", "cheaper_sibling": "b", "justification": "j", **fields}


def without(entry: dict, field: str) -> dict:
    return {key: value for key, value in entry.items() if key != field}


BAD_COHERENCE_ANSWERS = [
    *[
        pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, set_score(value, dimension)), id=f"{dimension}-score-{label}")
        for dimension in COHERENCE_DIMENSIONS
        for value, label in ((0, "0"), (6, "6"), (2.5, "2.5"), (True, "a-boolean"), ("4", "a-string"))
    ],
    *[pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, drop_justification(dimension)), id=f"{dimension}-justification-missing") for dimension in COHERENCE_DIMENSIONS],
    *[pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, set_justification(dimension, 5)), id=f"{dimension}-justification-not-a-string") for dimension in COHERENCE_DIMENSIONS],
    *[pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, drop_dimension(dimension)), id=f"{dimension}-missing") for dimension in COHERENCE_DIMENSIONS],
    pytest.param("not json either", id="not-json"),
    pytest.param("[]", id="a-list"),
    pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, set_field("scores", [])), id="scores-not-an-object"),
    pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, drop_field("summary")), id="summary-missing"),
    pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, set_field("summary", 3)), id="summary-not-a-string"),
    pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, drop_field("shadowing_risks")), id="risks-missing"),
    pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, set_field("shadowing_risks", {})), id="risks-not-a-list"),
    pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, set_field("shadowing_risks", ["a"])), id="a-risk-not-an-object"),
    *[pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, set_field("shadowing_risks", [without(risk(), field)])), id=f"a-risk-without-{field}") for field in ("tool", "cheaper_sibling", "justification")],
    *[pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, set_field("shadowing_risks", [risk(**{field: 1})])), id=f"a-risk-with-a-non-string-{field}") for field in ("tool", "cheaper_sibling", "justification")],
]


def test_a_good_coherence_answer_parses() -> None:
    parsed = tds.parse_coherence_answer(json.dumps(GOOD_COHERENCE_ANSWER))

    assert {dimension: parsed["scores"][dimension]["score"] for dimension in COHERENCE_DIMENSIONS} == dict(zip(COHERENCE_DIMENSIONS, (4, 3, 3, 3), strict=True))
    assert parsed["shadowing_risks"] == []


@pytest.mark.parametrize("text", BAD_COHERENCE_ANSWERS)
def test_a_bad_coherence_answer_is_refused(text: str) -> None:
    with pytest.raises(tds.InvalidAnswer):
        tds.parse_coherence_answer(text)


def test_the_retry_count_is_two() -> None:
    assert tds.RETRIES == 2


def test_an_invalid_tool_answer_is_retried() -> None:
    scorer = SequenceScorer(tool_answers=["this is not json", tool_answer((4, 4, 4, 4, 4, 4))])

    result = tds.evaluate_tool(tool("t", "Does t."), [], scorer)

    assert scorer.tool_calls == 2
    assert result["tdqs"] == Fraction("4.0")


def test_a_tool_answer_still_invalid_after_the_retries_makes_the_comparison_invalid() -> None:
    scorer = SequenceScorer(tool_answers=["this is not json"])

    with pytest.raises(tds.InvalidComparison, match="t_tool"):
        tds.evaluate_tool(tool("t_tool", "Does t."), [], scorer)

    assert scorer.tool_calls == 1 + tds.RETRIES


def test_a_backend_failure_is_retried_like_an_invalid_answer() -> None:
    scorer = SequenceScorer(tool_answers=[tds.BackendError("the model call failed"), tool_answer((4, 4, 4, 4, 4, 4))])

    result = tds.evaluate_tool(tool("t", "Does t."), [], scorer)

    assert scorer.tool_calls == 2
    assert result["tdqs"] == Fraction("4.0")


def test_a_backend_that_keeps_failing_makes_a_tool_invalid() -> None:
    scorer = SequenceScorer(tool_answers=[tds.BackendError("the model call failed")])

    with pytest.raises(tds.InvalidComparison):
        tds.evaluate_tool(tool("t", "Does t."), [], scorer)

    assert scorer.tool_calls == 1 + tds.RETRIES


def test_an_invalid_coherence_answer_is_retried() -> None:
    scorer = SequenceScorer(coherence_answers=["this is not json", coherence_answer((4, 4, 4, 4))])

    result = tds.evaluate_coherence("agentic-hil", [tool("a", "A."), tool("b", "B.")], scorer)

    assert scorer.coherence_calls == 2
    assert result["coherenceScore"] == Fraction("4.0")


def test_a_coherence_answer_still_invalid_after_the_retries_makes_the_comparison_invalid() -> None:
    scorer = SequenceScorer(coherence_answers=["this is not json"])

    with pytest.raises(tds.InvalidComparison):
        tds.evaluate_coherence("agentic-hil", [tool("a", "A."), tool("b", "B.")], scorer)

    assert scorer.coherence_calls == 1 + tds.RETRIES


def test_a_backend_that_keeps_failing_makes_coherence_invalid() -> None:
    scorer = SequenceScorer(coherence_answers=[tds.BackendError("the model call failed")])

    with pytest.raises(tds.InvalidComparison):
        tds.evaluate_coherence("agentic-hil", [tool("a", "A."), tool("b", "B.")], scorer)

    assert scorer.coherence_calls == 1 + tds.RETRIES


# --- shadowed tools (Shadowed tools, Running TDQS at scale) --------------------

SEVEN_NAMES = [f"key_{index}" for index in range(7)]
STATS_TOOLS = [
    tool("ping", "Check the service answers."),
    tool("get_stat", "Return one stat for a player and season.", FOUR_FLAT),
    tool("lookup", "Look a record up by seven keys.", scalars(*SEVEN_NAMES)),
    tool("query_panel", "Run a qualified query over the panel.", QUERY_PANEL),
]
CONFIRMED = {"tool": "query_panel", "cheaper_sibling": "get_stat", "justification": "Same class of answer, cheaper call."}
CONFIRMED_ENRICHED = {
    "tool": "query_panel",
    "invocationCost": 13,
    "cheaperSibling": "get_stat",
    "cheaperSiblingInvocationCost": 4,
    "justification": "Same class of answer, cheaper call.",
}


@pytest.mark.parametrize(
    ("cheap", "expensive", "candidate"),
    [(0, 4, True), (0, 3, False), (10, 19, False), (10, 20, True), (2, 6, True), (3, 6, False), (4, 8, True), (4, 4, False)],
)
def test_is_shadow_candidate(cheap: int, expensive: int, candidate: bool) -> None:
    """expensive >= 2 x cheap and expensive - cheap >= 4."""
    assert tds.is_shadow_candidate(cheap, expensive) is candidate


def test_the_prefilter_proposes_the_dearest_qualifying_sibling_once_per_tool() -> None:
    """Costs 0, 4, 7 and 13. query_panel qualifies against ping and get_stat and gets
    get_stat, the dearer; lookup (7 < 2 x 4) and get_stat fall back to the zero-argument ping."""
    candidates = tds.shadow_candidates(STATS_TOOLS)

    assert [(item["tool"], item["cheaperSibling"]) for item in candidates] == [("get_stat", "ping"), ("lookup", "ping"), ("query_panel", "get_stat")]
    query_panel = next(item for item in candidates if item["tool"] == "query_panel")
    assert (query_panel["invocationCost"], query_panel["cheaperSiblingInvocationCost"]) == (13, 4)


def test_the_prefilter_does_not_depend_on_tool_order() -> None:
    assert tds.shadow_candidates(list(reversed(STATS_TOOLS))) == tds.shadow_candidates(STATS_TOOLS)


def test_equal_costs_give_no_candidate() -> None:
    assert tds.shadow_candidates([tool("a", "A.", FOUR_FLAT), tool("b", "B.", FOUR_FLAT), tool("c", "C.", FOUR_FLAT)]) == []
    assert tds.shadow_candidates([tool("x", "X."), tool("y", "Y.")]) == []


def test_only_supplied_pairs_named_the_right_way_round_are_confirmed() -> None:
    candidates = tds.shadow_candidates(STATS_TOOLS)
    entries = [
        CONFIRMED,
        {"tool": "get_stat", "cheaper_sibling": "query_panel", "justification": "The dearer side is not the tool."},
        {"tool": "lookup", "cheaper_sibling": "get_stat", "justification": "A pair the prefilter did not supply."},
        {"tool": "no_such_tool", "cheaper_sibling": "ping", "justification": "A tool that does not exist."},
        {"tool": "get_stat", "cheaper_sibling": "no_such_tool", "justification": "A sibling that does not exist."},
    ]

    assert tds.confirmed_shadowing_risks(entries, candidates) == [CONFIRMED_ENRICHED]


def test_no_tool_is_confirmed_twice() -> None:
    candidates = tds.shadow_candidates(STATS_TOOLS)

    confirmed = tds.confirmed_shadowing_risks([CONFIRMED, dict(CONFIRMED)], candidates)

    assert [item["tool"] for item in confirmed] == ["query_panel"]


def test_coherence_evaluation_scores_the_set_and_confirms_against_the_prefilter() -> None:
    reversed_pair = {"tool": "get_stat", "cheaper_sibling": "query_panel", "justification": "Reversed."}
    scorer = FakeScorer(coherence=coherence_answer((4, 3, 3, 3), risks=[CONFIRMED, reversed_pair], marker="COHERENCE-MARK"))

    result = tds.evaluate_coherence("stats-api", STATS_TOOLS, scorer)

    assert len(scorer.coherence_calls) == 1
    assert scorer.coherence_calls[0][1] == tuple(item["name"] for item in STATS_TOOLS)
    assert scorer.coherence_calls[0][2] == tds.shadow_candidates(STATS_TOOLS)
    assert (result["disambiguation"], result["namingConsistency"], result["toolCountAppropriateness"], result["completeness"]) == (4, 3, 3, 3)
    assert result["coherenceScore"] == Fraction("3.3")
    assert result["coherenceTier"] == "B"
    assert result["shadowingRisks"] == [CONFIRMED_ENRICHED]
    assert result["coherenceSummary"] == "A coherence summary."
    assert "completeness justification COHERENCE-MARK" in json.dumps(result["coherenceJustifications"])


def test_score_side_scores_every_tool_and_flags_the_shadowed_one() -> None:
    table = {item["description"]: tool_answer((4, 2, 2, 3, 4, 2)) for item in STATS_TOOLS}
    table["Check the service answers."] = tool_answer((3, 2, 2, 3, 4, 2))
    table["Run a qualified query over the panel."] = tool_answer((5, 5, 5, 5, 5, 5))
    scorer = FakeScorer(table, coherence_answer((4, 3, 3, 3), risks=[CONFIRMED]))
    names = [item["name"] for item in STATS_TOOLS]
    exported = tds.export_from_tools(copy.deepcopy(STATS_TOOLS), server_name="stats-api", server_version="1")

    side = tds.score_side(exported, scorer, version_digest="d" * 64)

    assert sorted(side["tools"]) == sorted(names)
    assert side["setHash"] == exported["setHash"]
    assert side["versionDigest"] == "d" * 64
    assert len(scorer.tool_calls) == 4
    assert len(scorer.coherence_calls) == 1
    for name, siblings in scorer.tool_calls:
        assert sorted(siblings) == sorted(other for other in names if other != name)
    assert side["tools"]["query_panel"]["serverFlags"] == ["Shadowing Risk"]
    assert all(side["tools"][name]["serverFlags"] == [] for name in names if name != "query_panel")
    assert side["coherence"]["shadowingRisks"] == [CONFIRMED_ENRICHED]
    assert side["rollups"]["minTool"] == "ping"
    assert side["rollups"]["minTdqs"] == Fraction("2.6")
    assert side["rollups"]["descriptionQualityScore"] == Fraction("3.1")
    assert side["rollups"]["coherenceScore"] == Fraction("3.3")
    assert side["rollups"]["overallScore"] == Fraction("3.2")


# --- the confirmation procedure -----------------------------------------------


def side(mean: str, overall: str | None = None) -> dict:
    rollups = {"meanTdqs": Fraction(mean)}
    if overall is not None:
        rollups["overallScore"] = Fraction(overall)
    return {"rollups": rollups}


class PairRunner:
    """Hands out the given (base, head) mean TDQS one pair per call, and counts the calls."""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self.pairs = list(pairs)
        self.calls = 0

    def __call__(self) -> tuple[dict, dict]:
        base, head = self.pairs[self.calls]
        self.calls += 1
        return side(base), side(head)


def test_the_confirmation_procedure_is_the_versioned_one() -> None:
    assert tds.CONFIRMATION == CONFIRMATION_PROCEDURE


@pytest.mark.parametrize(
    ("pairs", "decision", "calls"),
    [
        pytest.param([("3.6", "3.7")], "pass", 1, id="an-improvement-passes-on-one-pair"),
        pytest.param([("3.7", "3.7")], "pass", 1, id="a-tie-passes-on-one-pair"),
        pytest.param([("3.7", "3.6")] * 5, "block", 3, id="a-drop-inside-tier-a-blocks-after-three-pairs"),
        pytest.param([("3.2", "3.1")] * 5, "block", 3, id="a-drop-inside-tier-b-blocks"),
        pytest.param([("3.7", "3.6"), ("3.6", "3.7"), ("3.6", "3.6")], "pass", 3, id="a-first-pair-drop-the-median-clears"),
        pytest.param([("3.7", "3.6"), ("3.8", "3.7"), ("3.6", "3.6")], "block", 3, id="a-first-pair-drop-the-median-confirms"),
        pytest.param([("3.9", "3.8"), ("3.0", "3.2"), ("3.1", "3.0")], "pass", 3, id="per-side-median-passes-where-two-pairs-drop"),
        pytest.param([("3.8", "3.0"), ("3.0", "3.1"), ("3.6", "3.7")], "block", 3, id="per-side-median-blocks-where-two-pairs-rise"),
    ],
)
def test_confirmation_procedure(pairs: list[tuple[str, str]], decision: str, calls: int) -> None:
    """One pair; on a drop exactly two more, then median head against median base, per side."""
    runner = PairRunner(pairs)

    result = tds.confirm(runner)

    assert result["decision"] == decision
    assert runner.calls == calls
    assert len(result["pairs"]) == calls


@pytest.mark.parametrize(
    ("pairs", "decision", "calls"),
    [
        pytest.param([(("3.4", "3.7"), ("3.4", "3.5"))], "pass", 1, id="the-overall-falls-the-mean-holds-one-pair-passes"),
        pytest.param([(("3.4", "3.5"), ("3.3", "3.7"))] * 3, "block", 3, id="the-overall-rises-the-mean-falls-and-blocks"),
        pytest.param(
            [(("3.4", "3.7"), ("3.3", "3.9")), (("3.4", "3.9"), ("3.4", "3.1")), (("3.4", "3.9"), ("3.5", "3.1"))],
            "pass",
            3,
            id="the-medians-of-the-mean-clear-what-the-overall-would-block",
        ),
    ],
)
def test_the_mean_decides_not_the_overall(pairs: list[tuple[tuple[str, str], tuple[str, str]]], decision: str, calls: int) -> None:
    """Each side carries a mean TDQS and an overall that disagree: the mean triggers the
    confirmation pairs and its per-side medians decide."""
    count = [0]

    def run_pair() -> tuple[dict, dict]:
        (base_mean, base_overall), (head_mean, head_overall) = pairs[count[0]]
        count[0] += 1
        return side(base_mean, base_overall), side(head_mean, head_overall)

    result = tds.confirm(run_pair)

    assert result["decision"] == decision
    assert count[0] == calls
    assert tds.decide(result["pairs"]) == decision


def test_an_invalid_pair_during_confirmation_never_passes() -> None:
    calls = []

    def run_pair() -> tuple[dict, dict]:
        calls.append(1)
        if len(calls) == 1:
            return side("3.7"), side("3.6")
        raise tds.InvalidComparison("the second pair could not be scored")

    with pytest.raises(tds.InvalidComparison):
        tds.confirm(run_pair)


ONE_BEFORE = [tool("alpha_tool", "Before.")]
ONE_AFTER = [tool("alpha_tool", "After.")]


@pytest.mark.parametrize(
    ("base_values", "head_values", "decision", "recorded", "unused"),
    [
        pytest.param(
            [4, 4, 4, 4], [3, 5, 3, 5], "block",
            [("4.0", "3.0"), ("4.0", "5.0"), ("4.0", "3.0")], [5],
            id="a-favourable-second-pair-then-a-blocking-median-and-an-unused-fourth",
        ),
        pytest.param([4, 4, 4], [3, 5, 4], "pass", [("4.0", "3.0"), ("4.0", "5.0"), ("4.0", "4.0")], [], id="the-median-clears-the-first-drop"),
        pytest.param([5, 3, 4], [3, 4, 4], "pass", [("5.0", "3.0"), ("3.0", "4.0"), ("4.0", "4.0")], [], id="per-side-medians-not-per-pair-differences"),
    ],
)
def test_noisy_scores_run_through_the_comparison(
    base_values: list[int], head_values: list[int], decision: str, recorded: list[tuple[str, str]], unused: list[int]
) -> None:
    """One tool per side: a tool scoring v in every dimension has a TDQS of v, and so a
    mean TDQS of v, which is what decides."""
    scorer = QueueScorer({"Before.": base_values, "After.": head_values})

    report = tds.compare(export(ONE_BEFORE), export(ONE_AFTER), scorer, standin_record())

    assert report["decision"] == decision
    assert mean_pairs(report) == [(Fraction(base), Fraction(head)) for base, head in recorded]
    assert scorer.queues["After."] == unused
    assert scorer.queues["Before."] == base_values[3:]


# --- the comparison and its report --------------------------------------------


def test_the_exit_codes() -> None:
    assert tds.EXIT_CODES == {"pass": 0, "block": 1, "invalid": 2}


def test_unchanged_definitions_pass_without_any_model_call() -> None:
    tools = [tool("alpha_tool", "Alpha."), tool("beta_tool", "Beta.", FOUR_FLAT)]
    scorer = FakeScorer()

    report = tds.compare(export(tools, "1.0"), export(tools, "2.0"), scorer, standin_record())

    assert report["decision"] == "pass"
    assert report["exitCode"] == 0
    assert scorer.tool_calls == []
    assert scorer.coherence_calls == []
    assert "unchanged" in report["reason"].lower()


def test_unchanged_definitions_pass_without_a_scorer_at_all() -> None:
    """No credential is needed when nothing changed: the scorer is never built."""
    tools = [tool("alpha_tool", "Alpha.")]

    report = tds.compare(export(tools), export(tools), None, standin_record())

    assert report["decision"] == "pass"
    assert report["exitCode"] == 0


def expected_tool_calls(tools: list[dict]) -> Counter:
    """One call per tool, carrying every other tool of the same side as its siblings."""
    names = [item["name"] for item in tools]
    return Counter((name, tuple(sorted(other for other in names if other != name))) for name in names)


COMPLETE_BASE = [tool("alpha_tool", "Alpha."), tool("beta_tool", "Beta.", FOUR_FLAT), tool("gamma_tool", "Gamma.")]


@pytest.mark.parametrize(
    "head",
    [
        pytest.param([*COMPLETE_BASE, tool("delta_tool", "Delta.")], id="a-tool-added"),
        pytest.param(COMPLETE_BASE[:2], id="a-tool-removed"),
        pytest.param([COMPLETE_BASE[0], tool("beta_tool", "Beta.", FOUR_FLAT, annotations={"readOnlyHint": True}), COMPLETE_BASE[2]], id="one-field-of-one-tool-changed"),
    ],
)
def test_any_change_scores_both_complete_sets(head: list[dict]) -> None:
    """Whatever changed, every tool of both sides is scored with that side's complete
    sibling list, and both sets get their coherence evaluation, never only the change."""
    scorer = FakeScorer(default=tool_answer((4,) * 6))

    report = tds.compare(export(COMPLETE_BASE), export(head), scorer, standin_record())

    assert report["decision"] == "pass"
    assert len(report["pairs"]) == 1
    observed = Counter((name, tuple(sorted(siblings))) for name, siblings in scorer.tool_calls)
    assert observed == expected_tool_calls(COMPLETE_BASE) + expected_tool_calls(head)
    assert Counter(call[1] for call in scorer.coherence_calls) == Counter([tuple(item["name"] for item in COMPLETE_BASE), tuple(item["name"] for item in head)])
    pair = report["pairs"][0]
    assert sorted(pair["base"]["tools"]) == sorted(item["name"] for item in COMPLETE_BASE)
    assert sorted(pair["head"]["tools"]) == sorted(item["name"] for item in head)


def exact_mean(scored_side: dict) -> Fraction:
    values = [result["tdqs"] for result in scored_side["tools"].values()]
    return sum(values, Fraction(0)) / len(values)


def test_a_new_tool_that_lowers_only_the_minimum_and_the_overall_passes() -> None:
    """Base: 3.5 and 3.6, coherence 4.0, overall 3.7. Head: both rise to 4.5 and a new
    tool at 2.5 joins; the minimum falls to 2.5, description quality to 3.3 and the
    overall to 3.5, but the mean TDQS rises from 3.6 (3.55) to 3.8 (3.83), and the
    mean decides: one pair, a pass, and the fallen overall shown as information."""
    base_tools = [tool("alpha_tool", "Alpha, before."), tool("beta_tool", "Beta, before.")]
    head_tools = [tool("alpha_tool", "Alpha, after."), tool("beta_tool", "Beta, after."), tool("new_tool", "New tool.")]
    scorer = FakeScorer(
        {
            "Alpha, before.": tool_answer((4, 3, 3, 3, 4, 4)),
            "Beta, before.": tool_answer((4, 3, 3, 4, 4, 4)),
            "Alpha, after.": tool_answer((5, 4, 4, 4, 5, 5)),
            "Beta, after.": tool_answer((5, 4, 4, 4, 5, 5)),
            "New tool.": tool_answer((3, 2, 2, 2, 3, 3)),
        }
    )

    report = tds.compare(export(base_tools), export(head_tools), scorer, standin_record())

    assert report["decision"] == "pass"
    assert report["exitCode"] == 0
    assert len(report["pairs"]) == 1
    assert len(scorer.tool_calls) == 2 + 3
    first = report["pairs"][0]
    assert first["base"]["rollups"]["overallScore"] == Fraction("3.7")
    assert first["head"]["rollups"]["overallScore"] == Fraction("3.5")
    assert first["head"]["rollups"]["descriptionQualityScore"] == Fraction("3.3")
    assert mean_pairs(report) == [(Fraction("3.6"), Fraction("3.8"))]
    assert report["causes"] is None
    assert report["reason"] == "The mean TDQS holds: 3.6 before, 3.8 after."
    summary = tds.summary_markdown(report)
    assert markdown_row(summary, "Mean TDQS", "3.6", "3.8")
    assert markdown_row(summary, "Overall", "3.7", "3.5")
    assert markdown_row(summary, "Description quality", "3.5", "3.3")
    minimum = re.search(r"^\|\s*Minimum TDQS\s*\|([^|]*)\|([^|]*)\|", summary, re.MULTILINE)
    assert minimum
    assert "new_tool" in minimum.group(2)


def test_the_mean_is_compared_as_its_rollup_rounded_once_like_every_score() -> None:
    """Base: two tools at 3.6. Head: 3.7 and 3.4, an exact mean of 3.55 that rounds half
    up to 3.6. The mean TDQS rollup is compared, so the head holds on one pair, while
    the minimum (3.4) pulls description quality down from 3.6 to 3.5."""
    base_tools = [tool("alpha_tool", "Alpha, before."), tool("beta_tool", "Beta, before.")]
    head_tools = [tool("alpha_tool", "Alpha, after."), tool("beta_tool", "Beta, after.")]
    scorer = FakeScorer(
        {
            "Alpha, before.": tool_answer((4, 4, 3, 4, 3, 3)),
            "Beta, before.": tool_answer((4, 4, 3, 4, 3, 3)),
            "Alpha, after.": tool_answer((4, 4, 3, 4, 3, 4)),
            "Beta, after.": tool_answer((3, 3, 4, 3, 4, 4)),
        }
    )

    report = tds.compare(export(base_tools), export(head_tools), scorer, standin_record())

    first = report["pairs"][0]
    assert sorted(result["tdqs"] for result in first["head"]["tools"].values()) == [Fraction("3.4"), Fraction("3.7")]
    assert exact_mean(first["head"]) < exact_mean(first["base"])
    assert mean_pairs(report) == [(Fraction("3.6"), Fraction("3.6"))]
    assert first["head"]["rollups"]["descriptionQualityScore"] < first["base"]["rollups"]["descriptionQualityScore"]
    assert report["decision"] == "pass"
    assert len(report["pairs"]) == 1


def test_a_confirmed_drop_names_the_mean_in_the_reason_and_the_report() -> None:
    """alpha_tool falls from 5.0 to 4.0 on every pair: the mean TDQS falls from 5.0 to 4.0,
    the three pairs and the medians are the mean's, and the tools that fell are ranked
    by what each fall alone costs the unrounded mean."""
    base_tools = [tool("alpha_tool", "Alpha, before.")]
    head_tools = [tool("alpha_tool", "Alpha, after.")]
    scorer = FakeScorer({"Alpha, before.": tool_answer((5,) * 6), "Alpha, after.": tool_answer((4,) * 6)})

    report = tds.compare(export(base_tools), export(head_tools), scorer, standin_record())

    assert report["decision"] == "block"
    assert report["reason"] == "The mean TDQS fell from 5.0 to 4.0, the median of each side over three pairs."
    assert report["median"] == {"base": Fraction("5.0"), "head": Fraction("4.0")}
    summary = tds.summary_markdown(report)
    assert "The mean TDQS decides" in summary
    assert re.search(r"^\|\s*Pair\s*\|\s*Base mean TDQS\s*\|\s*Head mean TDQS\s*\|", summary, re.MULTILINE)
    assert markdown_row(summary, "3", "5.0", "4.0")
    assert "Median mean TDQS: 5.0 before, 4.0 after." in summary
    assert "lowers the unrounded mean TDQS" in summary
    assert "overall" not in summary.split("### What fell on the first pair", 1)[1].split("### Size", 1)[0].lower()


def test_a_first_pair_drop_the_medians_clear_says_so_in_mean_terms() -> None:
    pairs = [(side("3.5"), side("3.4")), (side("3.4"), side("3.5")), (side("3.4"), side("3.4"))]

    assert tds.decide(pairs) == "pass"
    reason = tds._reason("pass", pairs)
    assert reason == (
        "The first pair's mean TDQS dropped from 3.5 to 3.4, but the medians over three pairs are "
        "3.4 before and 3.4 after, so the drop is not confirmed."
    )


def test_a_drop_names_the_dimensions_and_coherence_that_fell_with_their_justifications() -> None:
    """alpha_tool loses usage guidelines (5.0 to 4.4) and the set loses completeness
    (coherence 4.0 to 3.5): overall 4.2 to 3.9."""
    base_tools = [tool("alpha_tool", "Alpha, before."), tool("beta_tool", "Beta.")]
    head_tools = [tool("alpha_tool", "Alpha, after."), tool("beta_tool", "Beta.")]

    def coherence(tools: list) -> str:
        if any(item["description"] == "Alpha, after." for item in tools):
            return coherence_answer((4, 4, 4, 2), marker="COMPLETENESS-AFTER")
        return coherence_answer((4, 4, 4, 4), marker="COMPLETENESS-BEFORE")

    scorer = FakeScorer(
        {
            "Alpha, before.": tool_answer((5, 5, 5, 5, 5, 5), marker="ALPHA-BEFORE"),
            "Alpha, after.": tool_answer((5, 2, 5, 5, 5, 5), marker="ALPHA-AFTER"),
            "Beta.": tool_answer((4, 4, 4, 4, 4, 4)),
        },
        coherence,
    )

    report = tds.compare(export(base_tools), export(head_tools), scorer, standin_record())

    assert report["decision"] == "block"
    assert report["pairs"][0]["base"]["rollups"]["overallScore"] == Fraction("4.2")
    assert report["pairs"][0]["head"]["rollups"]["overallScore"] == Fraction("3.9")
    causes = report["causes"]
    assert [item["tool"] for item in causes["tools"]] == ["alpha_tool"]
    assert [item["dimension"] for item in causes["tools"][0]["dimensions"]] == ["usage_guidelines"]
    assert [item["dimension"] for item in causes["coherence"]] == ["completeness"]
    for scored_pair in report["pairs"]:
        for scored_side in (scored_pair["base"], scored_pair["head"]):
            assert all(set(result["justifications"]) == set(DIMENSIONS) for result in scored_side["tools"].values())
            assert "coherenceScore" in scored_side["coherence"]
            assert "overallScore" in scored_side["rollups"]
    summary = tds.summary_markdown(report)
    for expected in ("alpha_tool", "usage_guidelines", "usage_guidelines justification ALPHA-AFTER", "completeness", "completeness justification COMPLETENESS-AFTER"):
        assert expected in summary


def test_the_tools_that_fell_are_sorted_by_their_effect_on_the_mean() -> None:
    """zeta_tool falls 4.5 to 2.5 and becomes the minimum; beta_tool falls 4.5 to 4.3.
    The mean TDQS falls from 4.3 to 3.6."""
    base_tools = [tool("beta_tool", "Beta, before."), tool("gamma_tool", "Gamma."), tool("zeta_tool", "Zeta, before.")]
    head_tools = [tool("beta_tool", "Beta, after."), tool("gamma_tool", "Gamma."), tool("zeta_tool", "Zeta, after.")]
    scorer = FakeScorer(
        {
            "Beta, before.": tool_answer((5, 4, 4, 4, 5, 5)),
            "Beta, after.": tool_answer((5, 4, 4, 4, 4, 4)),
            "Gamma.": tool_answer((4, 4, 4, 4, 4, 4)),
            "Zeta, before.": tool_answer((5, 4, 4, 4, 5, 5)),
            "Zeta, after.": tool_answer((3, 2, 2, 2, 3, 3)),
        }
    )

    report = tds.compare(export(base_tools), export(head_tools), scorer, standin_record())

    assert report["decision"] == "block"
    assert mean_pairs(report)[0] == (Fraction("4.3"), Fraction("3.6"))
    assert [item["tool"] for item in report["causes"]["tools"]] == ["zeta_tool", "beta_tool"]
    assert [item["effect"] for item in report["causes"]["tools"]] == [Fraction(2, 3), Fraction(1, 15)]
    assert report["causes"]["minimumTerm"]["head"]["tool"] == "zeta_tool"


def test_the_bigger_fall_outranks_the_minimum_tool_on_the_mean() -> None:
    """Five tools. zulu_tool, the minimum on both sides, falls 3.0 to 2.8; alpha_tool falls
    4.5 to 3.9. The mean TDQS falls from 4.2 to 4.0 (4.04). Taken alone, each fall
    lowers the unrounded mean by its size over the five tools: alpha's by 0.6 / 5 = 0.12
    and zulu's by 0.2 / 5 = 0.04, so alpha comes first; being the minimum adds nothing."""
    others = ("bravo_tool", "charlie_tool", "delta_tool")
    base_tools = [tool("zulu_tool", "Zulu, before."), tool("alpha_tool", "Alpha, before."), *[tool(name, "Steady.") for name in others]]
    head_tools = [tool("zulu_tool", "Zulu, after."), tool("alpha_tool", "Alpha, after."), *[tool(name, "Steady.") for name in others]]
    scorer = FakeScorer(
        {
            "Zulu, before.": tool_answer((3, 3, 3, 3, 3, 3)),
            "Zulu, after.": tool_answer((3, 3, 3, 3, 2, 2)),
            "Alpha, before.": tool_answer((4, 5, 5, 4, 5, 4)),
            "Alpha, after.": tool_answer((4, 4, 4, 4, 4, 3)),
            "Steady.": tool_answer((4, 5, 5, 4, 5, 4)),
        }
    )

    report = tds.compare(export(base_tools), export(head_tools), scorer, standin_record())

    assert report["decision"] == "block"
    assert mean_pairs(report)[0] == (Fraction("4.2"), Fraction("4.0"))
    causes = report["causes"]["tools"]
    assert [item["tool"] for item in causes] == ["alpha_tool", "zulu_tool"]
    assert [item["effect"] for item in causes] == [Fraction("0.12"), Fraction("0.04")]
    assert [(item["base"], item["head"]) for item in causes] == [(Fraction("4.5"), Fraction("3.9")), (Fraction("3.0"), Fraction("2.8"))]
    assert report["causes"]["minimumTerm"]["base"]["tool"] == "zulu_tool"
    assert report["causes"]["minimumTerm"]["head"]["tool"] == "zulu_tool"


@pytest.mark.parametrize("total", [5, 44])
def test_an_answer_that_stays_invalid_makes_the_comparison_invalid(total: int) -> None:
    """One tool of many whose answer never validates is enough: no partial result passes."""
    fine = [tool(f"fine_{index:02d}", f"Fine {index}.") for index in range(total - 1)]
    base_tools = [tool("broken_tool", "Broken, before."), *fine]
    head_tools = [tool("broken_tool", "Broken, after."), *fine]
    scorer = FakeScorer({"Broken, after.": "this is not json"}, default=tool_answer((4, 4, 4, 4, 4, 4)))

    report = tds.compare(export(base_tools), export(head_tools), scorer, standin_record())

    assert report["decision"] == "invalid"
    assert report["exitCode"] == 2
    assert "broken_tool" in report["reason"]


def markdown_row(summary: str, label: str, base: str, head: str) -> re.Match | None:
    return re.search(rf"^\|\s*{re.escape(label)}\s*\|\s*{re.escape(base)}\b[^|]*\|\s*{re.escape(head)}\b[^|]*\|", summary, re.MULTILINE)


def test_the_report_carries_exports_versions_and_commits_and_serializes() -> None:
    base_tools = [tool("alpha_tool", "Alpha, before.")]
    head_tools = [tool("alpha_tool", "Alpha, after.")]
    scorer = FakeScorer({"Alpha, before.": tool_answer((4, 4, 4, 4, 4, 4)), "Alpha, after.": tool_answer((5, 5, 5, 5, 5, 5))})
    base_export, head_export = export(base_tools), export(head_tools)
    record = standin_record()

    report = tds.compare(base_export, head_export, scorer, record, base_commit="1" * 40, head_commit="2" * 40)

    assert report["decision"] == "pass"
    assert len(report["pairs"]) == 1
    assert report["base"]["export"] == base_export
    assert report["head"]["export"] == head_export
    assert report["versions"] == record
    assert report["versionDigest"] == tds.version_digest(record)
    assert (report["baseCommit"], report["headCommit"]) == ("1" * 40, "2" * 40)
    parsed = json.loads(tds.report_json(report))
    assert parsed["pairs"][0]["head"]["rollups"]["overallScore"] == 4.7
    assert parsed["pairs"][0]["base"]["tools"]["alpha_tool"]["tdqs"] == 4.0
    assert parsed["base"]["export"]["tools"] == base_tools
    summary = tds.summary_markdown(report)
    assert markdown_row(summary, "Overall", "4.0", "4.7")
    assert markdown_row(summary, "Description quality", "4.0", "5.0")
    assert markdown_row(summary, "Coherence", "4.0", "4.0")
    assert markdown_row(summary, "Mean TDQS", "4.0", "5.0")
    assert report["reason"] == "The mean TDQS holds: 4.0 before, 5.0 after."
    assert "The mean TDQS decides" in summary
    minimum = re.search(r"^\|\s*Minimum TDQS\s*\|([^|]*)\|([^|]*)\|", summary, re.MULTILINE)
    assert minimum
    assert "4.0" in minimum.group(1) and "alpha_tool" in minimum.group(1)
    assert "5.0" in minimum.group(2) and "alpha_tool" in minimum.group(2)


def test_a_stale_report_is_rejected() -> None:
    tools = [tool("alpha_tool", "Alpha.")]
    base_export, head_export = export(tools), export(tools)
    record = standin_record()
    report = tds.compare(base_export, head_export, None, record)

    tds.check_report(report, base_export, head_export, record)
    with pytest.raises(tds.StaleReport):
        tds.check_report(report, base_export, export([tool("alpha_tool", "Alpha, reworded.")]), record)
    with pytest.raises(tds.StaleReport):
        tds.check_report(report, export([tool("alpha_tool", "Alpha, older.")]), head_export, record)
    with pytest.raises(tds.StaleReport):
        tds.check_report(report, base_export, head_export, standin_record(model="another-model"))
    assert issubclass(tds.StaleReport, tds.InvalidComparison)


def scored_report() -> tuple[dict, dict, dict, dict]:
    """A complete, scored, passing report over two changed tools, with its exports and record."""
    base_tools = [tool("alpha_tool", "Alpha, before."), tool("beta_tool", "Beta, before.")]
    head_tools = [tool("alpha_tool", "Alpha, after."), tool("beta_tool", "Beta, after.")]
    table = {
        "Alpha, before.": tool_answer((4,) * 6),
        "Beta, before.": tool_answer((4,) * 6),
        "Alpha, after.": tool_answer((5,) * 6),
        "Beta, after.": tool_answer((5,) * 6),
    }
    base_export, head_export, record = export(base_tools), export(head_tools), standin_record()
    report = tds.compare(base_export, head_export, FakeScorer(table), record)
    assert report["decision"] == "pass"
    return report, base_export, head_export, record


def first_head_tool(report: dict) -> dict:
    return report["pairs"][0]["head"]["tools"]["alpha_tool"]


REPORT_MUTATIONS = [
    pytest.param(lambda r: r["pairs"][0]["head"]["tools"].pop("beta_tool"), tds.IncompleteResult, id="a-scored-tool-missing"),
    pytest.param(lambda r: r["pairs"][0]["base"].pop("coherence"), tds.IncompleteResult, id="coherence-missing"),
    pytest.param(lambda r: r["pairs"].clear(), tds.IncompleteResult, id="the-pairs-missing"),
    pytest.param(lambda r: first_head_tool(r).__setitem__("definitionHash", "0" * 64), tds.StaleReport, id="a-stale-tool-hash"),
    pytest.param(lambda r: r["pairs"][0]["base"].__setitem__("versionDigest", "0" * 64), tds.StaleReport, id="a-side-scored-under-other-versions"),
    pytest.param(lambda r: r["pairs"][0]["head"].__setitem__("setHash", "0" * 64), tds.StaleReport, id="a-side-scored-from-another-set"),
    pytest.param(lambda r: r["versions"].__setitem__("retries", 3), tds.StaleReport, id="the-versions-changed"),
    pytest.param(lambda r: r.__setitem__("decision", "block"), tds.InvalidComparison, id="the-decision-flipped"),
    pytest.param(lambda r: r.__setitem__("exitCode", 1), tds.InvalidComparison, id="the-exit-code-changed"),
    pytest.param(lambda r: first_head_tool(r).__setitem__("tdqs", Fraction("4.9")), tds.InvalidComparison, id="a-tdqs-changed"),
    pytest.param(lambda r: first_head_tool(r)["scores"].__setitem__("purpose_clarity", 4), tds.InvalidComparison, id="a-dimension-score-changed"),
    pytest.param(lambda r: r["pairs"][0]["head"]["rollups"].__setitem__("overallScore", Fraction("4.9")), tds.InvalidComparison, id="an-overall-changed"),
    pytest.param(lambda r: r["pairs"][0]["head"]["coherence"].__setitem__("coherenceScore", Fraction("4.9")), tds.InvalidComparison, id="a-coherence-score-changed"),
    pytest.param(lambda r: r["pairs"].append(copy.deepcopy(r["pairs"][0])), tds.InvalidComparison, id="two-pairs"),
]


@pytest.mark.parametrize(("mutate", "error"), REPORT_MUTATIONS)
def test_a_scored_report_that_does_not_hold_together_is_rejected(mutate: Callable[[dict], object], error: type[Exception]) -> None:
    """Each mutation leaves the report's outer exports untouched, so only the scored
    content itself can give it away."""
    report, base_export, head_export, record = scored_report()
    tds.check_report(report, base_export, head_export, record)
    mutated = copy.deepcopy(report)
    mutate(mutated)
    assert mutated["base"]["export"] == base_export
    assert mutated["head"]["export"] == head_export

    with pytest.raises(error):
        tds.check_report(mutated, base_export, head_export, record)


def test_a_scored_report_survives_its_own_json() -> None:
    report, base_export, head_export, record = scored_report()

    tds.check_report(json.loads(tds.report_json(report)), base_export, head_export, record)


# --- the version record and the calibration record ----------------------------


def test_the_committed_version_record_pins_model_spec_prompts_and_rubric() -> None:
    record = tds.load_version_record()

    assert record["model"] == PINNED_MODEL
    assert record["cli_version"] == PINNED_CLI_VERSION
    assert re.fullmatch(r"[0-9a-f]{40}", record["spec_commit"])
    assert record["spec_commit"].startswith("b9881b0cfec8")
    assert record["spec_version"] == "1.3"
    assert set(record["prompt_sha256"]) == PROMPT_KEYS
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in record["prompt_sha256"].values())
    assert record["dimension_weights"] == PUBLISHED_WEIGHTS == dict(tds.DIMENSION_WEIGHTS)
    assert record["description_quality_weights"] == {"mean": 60, "minimum": 40}
    assert record["overall_weights"] == {"description_quality": 70, "coherence": 30}
    assert {letter: Fraction(str(value)) for letter, value in record["tier_thresholds"].items()} == {
        "A": Fraction("3.5"),
        "B": Fraction("3.0"),
        "C": Fraction("2.0"),
        "D": Fraction("1.0"),
    }
    assert record["retries"] == tds.RETRIES
    assert record["confirmation"] == CONFIRMATION_PROCEDURE == tds.CONFIRMATION


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda r: r.__setitem__("model", "another-model"), id="model"),
        pytest.param(lambda r: r.__setitem__("cli_version", "9.9.9"), id="cli-version"),
        pytest.param(lambda r: r.__setitem__("spec_commit", "f" * 40), id="spec-commit"),
        pytest.param(lambda r: r.__setitem__("spec_version", "1.4"), id="spec-version"),
        pytest.param(lambda r: r["prompt_sha256"].__setitem__("coherence_user", "0" * 64), id="a-prompt-hash"),
        pytest.param(lambda r: r["dimension_weights"].__setitem__("purpose_clarity", 30), id="a-weight"),
        pytest.param(lambda r: r["description_quality_weights"].__setitem__("mean", 61), id="a-description-quality-weight"),
        pytest.param(lambda r: r["overall_weights"].__setitem__("coherence", 31), id="a-rollup-weight"),
        pytest.param(lambda r: r["tier_thresholds"].__setitem__("A", 3.6), id="a-tier-threshold"),
        pytest.param(lambda r: r.__setitem__("retries", 3), id="the-retry-count"),
        pytest.param(lambda r: r["confirmation"].__setitem__("version", 2), id="the-confirmation-version"),
        pytest.param(lambda r: r["confirmation"].__setitem__("confirmation_pairs_on_drop", 3), id="the-confirmation-pair-count"),
    ],
)
def test_the_version_digest_moves_with_every_field(change: Callable[[dict], object]) -> None:
    record = standin_record()
    changed = copy.deepcopy(record)
    change(changed)

    assert re.fullmatch(r"[0-9a-f]{64}", tds.version_digest(record))
    assert tds.version_digest(reversed_keys(record)) == tds.version_digest(record)  # type: ignore[arg-type]
    assert tds.version_digest(changed) != tds.version_digest(record)


def per_dimension(values: tuple[int, ...]) -> dict:
    return {"scores": scores(*values), "tdqs": float(tds.compute_tdqs(scores(*values)))}


def test_the_calibration_summary_compares_per_tool_and_per_dimension() -> None:
    """alpha_tool: the registry 4.0, three runs 4.3, 4.0 and 4.5, so the median 4.3 and
    +0.3; purpose clarity 4 against 5, 4, 5, so +1. beta_tool: the registry 3.0, runs
    3.0, 2.8 and 2.6, so 2.8 and -0.2; purpose clarity 3 against 3, 2, 2, so -1.
    The mean absolute difference is 0.25 on the TDQS, 1 on purpose clarity, 0 elsewhere."""
    registry = {"alpha_tool": per_dimension((4,) * 6), "beta_tool": per_dimension((3,) * 6)}
    runs = [
        {"alpha_tool": per_dimension((5, 4, 4, 4, 4, 4)), "beta_tool": per_dimension((3,) * 6)},
        {"alpha_tool": per_dimension((4,) * 6), "beta_tool": per_dimension((2, 3, 3, 3, 3, 3))},
        {"alpha_tool": per_dimension((5, 5, 4, 4, 4, 4)), "beta_tool": per_dimension((2, 2, 3, 3, 3, 3))},
    ]

    summary = tds.calibration_summary(registry, runs)

    alpha, beta = summary["tools"]["alpha_tool"], summary["tools"]["beta_tool"]
    assert alpha["gate"]["tdqs"] == Fraction("4.3")
    assert alpha["difference"]["tdqs"] == Fraction("0.3")
    assert alpha["gate"]["purpose_clarity"] == 5
    assert alpha["difference"]["purpose_clarity"] == 1
    assert alpha["difference"]["usage_guidelines"] == 0
    assert beta["gate"]["tdqs"] == Fraction("2.8")
    assert beta["difference"]["tdqs"] == Fraction("-0.2")
    assert beta["difference"]["purpose_clarity"] == -1
    assert beta["difference"]["usage_guidelines"] == 0
    assert summary["meanAbsoluteDifference"] == {"tdqs": Fraction("0.25"), "purpose_clarity": 1, **dict.fromkeys(DIMENSIONS[1:], 0)}
    assert [item["tool"] for item in summary["largestDifferences"]] == ["alpha_tool", "beta_tool"]


def test_a_published_definition_is_compared_field_by_field_and_key_by_key() -> None:
    """The registry may publish a definition with less than the server sends: each
    difference is named down to the key of an object field."""
    sent = [
        tool("alpha_tool", annotations={"title": "Alpha", "readOnlyHint": True}),
        tool("beta_tool", "Beta text."),
        tool("gamma_tool"),
    ]
    published = [
        tool("alpha_tool", annotations={"readOnlyHint": True}),
        tool("beta_tool", "Other text.", title=None),
        tool("gamma_tool"),
    ]

    assert tds.definition_differences(published, sent) == {"alpha_tool": ["annotations.title"], "beta_tool": ["description"]}


def test_the_calibration_scores_the_published_definitions_in_listing_order() -> None:
    sent = export([tool("beta_tool", annotations={"title": "Beta"}), tool("alpha_tool")])
    published = [tool("alpha_tool"), tool("beta_tool", annotations={})]

    scored = tds.calibration_export(published, sent)

    assert [item["name"] for item in scored["tools"]] == ["beta_tool", "alpha_tool"]
    assert scored["setHash"] == tds.set_hash(published) != sent["setHash"]


def test_a_registry_with_other_tools_cannot_be_calibrated_against() -> None:
    with pytest.raises(tds.InvalidComparison):
        tds.calibration_export([tool("alpha_tool")], export([tool("alpha_tool"), tool("beta_tool")]))


def test_the_calibration_record_refuses_a_run_over_other_definitions() -> None:
    published = [tool("alpha_tool", annotations={"readOnlyHint": True})]
    sent = export([tool("alpha_tool", annotations={"title": "Alpha", "readOnlyHint": True})])
    registry = {"release": "1.0", "server": {}, "tools": {"alpha_tool": per_dimension((4,) * 6)}}
    scorer = FakeScorer(default=tool_answer((4,) * 6))
    record = standin_record()
    right = tds.score_side(tds.calibration_export(published, sent), scorer, tds.version_digest(record))
    wrong = tds.score_side(sent, scorer, tds.version_digest(record))

    calibration = tds.calibration_record(record, registry, published, "0" * 40, sent, [right] * 3)

    assert calibration["evaluated"]["setHash"] == calibration["registry"]["setHash"] != calibration["evaluated"]["commitSetHash"]
    assert calibration["evaluated"]["definitionDifferences"] == {"alpha_tool": ["annotations.title"]}
    with pytest.raises(tds.InvalidComparison):
        tds.calibration_record(record, registry, published, "0" * 40, sent, [right, right, wrong])


def test_the_calibration_record_belongs_to_this_version_record() -> None:
    """A change to anything in the version record needs a new calibration."""
    assert tds.load_calibration_record()["version_digest"] == tds.version_digest(tds.load_version_record())


def test_the_committed_calibration_record_holds_together() -> None:
    """The registry's published scores, captured with their source and release; the
    definitions the registry scored are the ones the evaluator scored; three runs over
    the same tools, each internally consistent; and the summary recomputes from them."""
    calibration = tds.load_calibration_record()
    registry = calibration["registry"]

    assert registry["source"].startswith("https://glama.ai/mcp/servers/agentic-hil/agentic-hil")
    assert registry["release"] == "0.22.1-dev.0"
    assert registry["setHash"] == calibration["evaluated"]["setHash"]
    assert (calibration["evaluated"]["commitSetHash"] == registry["setHash"]) == (not calibration["evaluated"]["definitionDifferences"])
    assert re.fullmatch(r"[0-9a-f]{40}", calibration["evaluated"]["commit"])
    assert len(registry["tools"]) == calibration["evaluated"]["toolCount"] == 44
    assert len(calibration["runs"]) == 3
    for run in calibration["runs"]:
        assert set(run["tools"]) == set(registry["tools"])
        for result in run["tools"].values():
            assert Fraction(str(result["tdqs"])) == tds.compute_tdqs(result["scores"])
        coherence = {"coherenceScore": tds.coherence_score({dimension: run["coherence"][dimension] for dimension in COHERENCE_DIMENSIONS})}
        tdqs_by_tool = {name: {"tdqs": Fraction(str(result["tdqs"]))} for name, result in run["tools"].items()}
        recomputed = tds.rollups(tdqs_by_tool, list(run["tools"]), coherence)
        for key in ("meanTdqs", "minTdqs", "descriptionQualityScore", "coherenceScore", "overallScore"):
            assert Fraction(str(run["rollups"][key])) == recomputed[key], key
    expected = tds.calibration_summary(registry["tools"], [run["tools"] for run in calibration["runs"]])
    assert calibration["summary"] == json.loads(tds.report_json(expected))
    document = CALIBRATION_DOCUMENT.read_text(encoding="utf-8")
    assert "mean absolute difference" in document.lower()
    assert f"{calibration['summary']['meanAbsoluteDifference']['tdqs']:.2f}" in document


def test_the_document_names_the_pinned_model_and_command_line() -> None:
    """The page that explains the scores names the judge that gives them, and no other."""
    record = tds.load_version_record()
    document = CALIBRATION_DOCUMENT.read_text(encoding="utf-8")

    assert f"`{record['model']}`" in document
    assert f"command line {record['cli_version']}" in document
    assert f"calibrated with {record['cli_version']}" in document
    assert set(re.findall(r"\b2\.\d+\.\d{3}\b", document)) == {record["cli_version"]}
    assert set(re.findall(r"`(claude-[a-z0-9-]+)`", document)) == {record["model"]}


# --- the prompts: fetched, extracted, verified, never committed ---------------

STANDIN_TOOL_SYSTEM = "STAND-IN TOOL SYSTEM PROMPT\n\n## A heading inside the fence\nScore {name} on six dimensions."
# The stand-in user templates use the published placeholder grammar with other wording.
STANDIN_TOOL_USER = "\n".join(
    [
        "Tool {name}",
        'Display title {title | "null"}',
        'Text: "{description}"',
        "[schema]",
        '{inputSchema JSON | "{}"}',
        "[output]",
        '{outputSchema JSON | "None provided"}',
        "[annotations]",
        '{annotations JSON | "None provided"}',
        "Counts: {paramCount} params, {requiredParamCount} required, {schemaDescriptionCoverage}% described, {paramsWithEnums} with enums, nested {hasNestedObjects}",
        "[siblings]",
        '{sibling tool names, one per line | "None"}',
        "JSON only.",
    ]
)
STANDIN_COHERENCE_SYSTEM = "STAND-IN COHERENCE SYSTEM PROMPT\n## Another heading inside the fence"
STANDIN_COHERENCE_USER = "\n".join(
    [
        "Server {serverName} with {toolCount} tools",
        "[tools]",
        '- {name} (cost {invocationCost}; {requiredFieldCount} req, depth {schemaDepth}, {unionChoiceCount} unions): {description | "(no description)"}',
        "- ...",
        "[candidates]",
        '{"{dearer} costs {n}, {cheaper} costs {m}", one per line | "None"}',
        "JSON only.",
    ]
)
STANDIN_PROMPTS = {
    "tool_system": STANDIN_TOOL_SYSTEM,
    "tool_user": STANDIN_TOOL_USER,
    "coherence_system": STANDIN_COHERENCE_SYSTEM,
    "coherence_user": STANDIN_COHERENCE_USER,
}


def fenced(text: str) -> str:
    return f"```text\n{text}\n```"


STANDIN_README = "\n".join(
    [
        "# Stand-in specification",
        "",
        "Prose before the appendices, with a block that is not a prompt:",
        "",
        fenced("DECOY BLOCK, NOT A PROMPT"),
        "",
        "## Appendix A: Tool scoring prompt",
        "",
        "The system prompt, verbatim:",
        "",
        fenced(STANDIN_TOOL_SYSTEM),
        "",
        "The user message template:",
        "",
        fenced(STANDIN_TOOL_USER),
        "",
        "## Appendix B: Server coherence prompt",
        "",
        fenced(STANDIN_COHERENCE_SYSTEM),
        "",
        fenced(STANDIN_COHERENCE_USER),
        "",
        "## References",
        "",
    ]
)
STANDIN_HASHES = {key: hashlib.sha256(text.encode("utf-8")).hexdigest() for key, text in STANDIN_PROMPTS.items()}
STANDIN_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def standin_prompt_record(**hashes: str) -> dict:
    return standin_record(spec_commit=STANDIN_COMMIT, prompt_sha256={**STANDIN_HASHES, **hashes})


def test_the_readme_is_fetched_at_the_full_commit() -> None:
    assert tds.upstream_readme_url(STANDIN_COMMIT) == f"https://raw.githubusercontent.com/glama-ai/tool-definition-quality-score/{STANDIN_COMMIT}/README.md"


def test_the_four_prompts_are_extracted_from_the_appendices() -> None:
    assert tds.extract_prompts(STANDIN_README) == STANDIN_PROMPTS


def test_a_readme_without_an_appendix_is_refused() -> None:
    truncated = STANDIN_README.split("## Appendix B")[0]

    with pytest.raises(tds.PromptMismatch):
        tds.extract_prompts(truncated)


def test_prompts_that_match_their_hashes_are_accepted() -> None:
    tds.verify_prompts(STANDIN_PROMPTS, STANDIN_HASHES)


@pytest.mark.parametrize("key", sorted(PROMPT_KEYS))
def test_each_changed_prompt_is_refused_and_named(key: str) -> None:
    changed = {**STANDIN_PROMPTS, key: STANDIN_PROMPTS[key] + " "}

    with pytest.raises(tds.PromptMismatch, match=key):
        tds.verify_prompts(changed, STANDIN_HASHES)


@pytest.mark.parametrize("key", sorted(PROMPT_KEYS))
def test_a_missing_prompt_or_a_missing_hash_is_refused_and_named(key: str) -> None:
    with pytest.raises(tds.PromptMismatch, match=key):
        tds.verify_prompts({name: text for name, text in STANDIN_PROMPTS.items() if name != key}, STANDIN_HASHES)
    with pytest.raises(tds.PromptMismatch, match=key):
        tds.verify_prompts(STANDIN_PROMPTS, {name: digest for name, digest in STANDIN_HASHES.items() if name != key})


def test_load_prompts_fetches_once_and_then_reads_the_cache(tmp_path: Path) -> None:
    record = standin_prompt_record()
    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return STANDIN_README

    def no_network(url: str) -> str:
        raise AssertionError(f"fetched {url} although the cache holds it")

    assert tds.load_prompts(record, tmp_path / "cache", fetch) == STANDIN_PROMPTS
    assert fetched == [tds.upstream_readme_url(STANDIN_COMMIT)]
    assert tds.load_prompts(record, tmp_path / "cache", no_network) == STANDIN_PROMPTS


@pytest.mark.parametrize("key", sorted(PROMPT_KEYS))
def test_load_prompts_refuses_any_changed_prompt_and_does_not_keep_it(tmp_path: Path, key: str) -> None:
    altered = STANDIN_README.replace(STANDIN_PROMPTS[key], STANDIN_PROMPTS[key] + "\nAN ADDED LINE")
    assert altered != STANDIN_README
    fetched: list[str] = []

    with pytest.raises(tds.PromptMismatch, match=key):
        tds.load_prompts(standin_prompt_record(), tmp_path / "cache", lambda url: altered)

    assert tds.load_prompts(standin_prompt_record(), tmp_path / "cache", lambda url: fetched.append(url) or STANDIN_README) == STANDIN_PROMPTS
    assert len(fetched) == 1


def cache_contents(cache: Path) -> dict[str, bytes]:
    return {path.relative_to(cache).as_posix(): path.read_bytes() for path in sorted(cache.rglob("*")) if path.is_file()}


def test_a_corrupted_cache_is_fetched_again(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    tds.load_prompts(standin_prompt_record(), cache, lambda url: STANDIN_README)
    for path in cache.rglob("*"):
        if path.is_file():
            path.write_text("CORRUPTED", encoding="utf-8")
    fetched: list[str] = []

    prompts = tds.load_prompts(standin_prompt_record(), cache, lambda url: fetched.append(url) or STANDIN_README)

    assert prompts == STANDIN_PROMPTS
    assert len(fetched) == 1
    assert tds.load_prompts(standin_prompt_record(), cache, lambda url: (_ for _ in ()).throw(AssertionError(url))) == STANDIN_PROMPTS


def test_changed_hashes_in_the_record_refuse_a_cache_that_matched_the_old_ones(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    tds.load_prompts(standin_prompt_record(), cache, lambda url: STANDIN_README)
    before = cache_contents(cache)

    with pytest.raises(tds.PromptMismatch, match="tool_user"):
        tds.load_prompts(standin_prompt_record(tool_user="0" * 64), cache, lambda url: STANDIN_README)

    assert cache_contents(cache) == before


def test_the_default_cache_is_outside_the_repository() -> None:
    assert not tds.default_cache_dir().resolve().is_relative_to(ROOT)
    assert not tds.default_cache_dir(dict(os.environ)).resolve().is_relative_to(ROOT)


# --- the prompts as the model receives them (Appendix A, Appendix B) ----------

READ_THING = tool(
    "read_thing",
    "Read one thing by its id; waits up to 5 µs.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Thing id."},
            "mode": {"type": "string", "enum": ["fast", "slow"]},
            "filter": {"type": "object", "properties": {"tag": {"type": "string"}}},
        },
        "required": ["id"],
    },
    title="Read a thing",
    annotations={"readOnlyHint": True},
)
READ_THING_PROMPT = """Tool read_thing
Display title Read a thing
Text: "Read one thing by its id; waits up to 5 µs."
[schema]
{
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "description": "Thing id."
    },
    "mode": {
      "type": "string",
      "enum": [
        "fast",
        "slow"
      ]
    },
    "filter": {
      "type": "object",
      "properties": {
        "tag": {
          "type": "string"
        }
      }
    }
  },
  "required": [
    "id"
  ]
}
[output]
None provided
[annotations]
{
  "readOnlyHint": true
}
Counts: 3 params, 1 required, 33% described, 1 with enums, nested true
[siblings]
list_things
ping
JSON only."""
BARE_PROMPT = """Tool ping
Display title null
Text: "Ping."
[schema]
{}
[output]
None provided
[annotations]
None provided
Counts: 0 params, 0 required, 100% described, 0 with enums, nested false
[siblings]
None
JSON only."""
STATS_PROMPT = """Server stats-api with 4 tools
[tools]
- ping (cost 0; 0 req, depth 0, 0 unions): Check the service answers.
- get_stat (cost 4; 4 req, depth 1, 0 unions): Return one stat for a player and season.
- lookup (cost 7; 7 req, depth 1, 0 unions): Look a record up by seven keys.
- query_panel (cost 13; 5 req, depth 3, 2 unions): Run a qualified query over the panel.
[candidates]
get_stat costs 4, ping costs 0
lookup costs 7, ping costs 0
query_panel costs 13, get_stat costs 4
JSON only."""


def test_the_tool_prompt_carries_the_full_definition_and_the_siblings() -> None:
    """The schema and the annotations go in whole, pretty-printed, non-ASCII kept; the
    signals are the five the template names; the siblings are one per line."""
    assert tds.render_tool_prompt(STANDIN_TOOL_USER, READ_THING, ["list_things", "ping"]) == READ_THING_PROMPT


@pytest.mark.parametrize("title", ["absent", None, "   "])
def test_the_tool_prompt_falls_back_where_a_field_is_missing(title: object) -> None:
    definition: dict = {"name": "ping", "description": "Ping."}
    if title != "absent":
        definition["title"] = title

    assert tds.render_tool_prompt(STANDIN_TOOL_USER, definition, []) == BARE_PROMPT


def test_the_output_schema_is_rendered_when_there_is_one() -> None:
    definition = tool("ping", "Ping.", outputSchema={"type": "object"})

    rendered = tds.render_tool_prompt(STANDIN_TOOL_USER, definition, [])

    assert '[output]\n{\n  "type": "object"\n}\n[annotations]' in rendered


@pytest.mark.parametrize("token", ["invocationCost", "requiredFieldCount", "schemaDepth", "unionChoiceCount", "definitionBytes", "hasOutputSchema", "favouriteColour"])
def test_a_tool_template_asking_for_a_withheld_or_unknown_signal_is_refused(token: str) -> None:
    """The invocation-cost signals, definitionBytes and hasOutputSchema are withheld
    from the tool prompt; a template that asks for them is not the published one."""
    with pytest.raises(tds.PromptMismatch, match=token):
        tds.render_tool_prompt(STANDIN_TOOL_USER + f"\n{{{token}}}", READ_THING, [])


def test_the_coherence_prompt_lists_every_tool_with_its_cost_and_the_candidates() -> None:
    candidates = tds.shadow_candidates(STATS_TOOLS)

    assert tds.render_coherence_prompt(STANDIN_COHERENCE_USER, "stats-api", STATS_TOOLS, candidates) == STATS_PROMPT


def test_the_coherence_prompt_falls_back_where_there_is_nothing() -> None:
    tools = [tool("ping", "Ping."), {"name": "ghost", "inputSchema": {"type": "object", "properties": {}}}]

    rendered = tds.render_coherence_prompt(STANDIN_COHERENCE_USER, "agentic-hil", tools, [])

    assert rendered == "\n".join(
        [
            "Server agentic-hil with 2 tools",
            "[tools]",
            "- ping (cost 0; 0 req, depth 0, 0 unions): Ping.",
            "- ghost (cost 0; 0 req, depth 0, 0 unions): (no description)",
            "[candidates]",
            "None",
            "JSON only.",
        ]
    )


def test_a_coherence_template_with_an_unknown_placeholder_is_refused() -> None:
    with pytest.raises(tds.PromptMismatch, match="favouriteColour"):
        tds.render_coherence_prompt(STANDIN_COHERENCE_USER + "\n{favouriteColour}", "stats-api", STATS_TOOLS, [])


# --- the model backend: the Claude Code command line ----------------------------

FAKE_CLAUDE = '''
import json, os, sys, time, uuid

arguments = sys.argv[1:]
if arguments == ["--version"]:
    print(os.environ.get("FAKE_CLAUDE_VERSION", "0.0.0") + " (Claude Code)")
    sys.exit(0)
started = time.time()
stdin = sys.stdin.buffer.read().decode("utf-8")
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "slow":
    time.sleep(0.5)
if mode == "hang":
    time.sleep(120)
cwd = os.getcwd()
entry = {"argv": arguments, "cwd": cwd, "listing": sorted(os.listdir(cwd)), "stdin": stdin, "env": dict(os.environ), "started": started, "ended": time.time()}
config = os.environ.get("CLAUDE_CONFIG_DIR")
if config:
    entry["configListing"] = sorted(os.listdir(config)) if os.path.isdir(config) else None
log = os.path.join(os.environ["FAKE_CLAUDE_LOG"], uuid.uuid4().hex + ".json")
with open(log, "w", encoding="utf-8") as handle:
    json.dump(entry, handle)
model = arguments[arguments.index("--model") + 1] if "--model" in arguments else "a-default-model"
answer = os.environ.get("FAKE_CLAUDE_ANSWER", "")
if mode == "garbage":
    print("this is not an envelope")
elif mode == "nonzero":
    sys.stderr.write("something went wrong\\n")
    sys.exit(3)
elif mode == "leak":
    sys.stderr.write("refused the token " + os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "") + "\\n")
    sys.exit(1)
else:
    usage = {"another-model" if mode == "wrong-model" else model: {"inputTokens": 1, "outputTokens": 1}}
    print(json.dumps({"type": "result", "subtype": "success", "is_error": mode == "error", "result": answer, "modelUsage": usage}))
'''
DUMMY_TOKEN = "dummy-token-value-0123456789"


@pytest.fixture
def fake_cli(tmp_path: Path) -> dict:
    """A stand-in for `claude`: it logs what it was given, one file per call, and answers
    in the CLI's JSON envelope."""
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    log = tmp_path / "log"
    log.mkdir()
    environ = {**os.environ, "FAKE_CLAUDE_LOG": str(log), "FAKE_CLAUDE_MODE": "ok", "FAKE_CLAUDE_ANSWER": tool_answer((4,) * 6), "FAKE_CLAUDE_VERSION": "0.0.0"}
    return {"command": [sys.executable, str(script)], "log": log, "environ": environ}


def cli_calls(log: Path) -> list[dict]:
    return sorted((json.loads(path.read_text(encoding="utf-8")) for path in log.glob("*.json")), key=lambda entry: entry["started"])


def option(argv: list[str], name: str) -> str:
    assert name in argv, (name, argv)
    return argv[argv.index(name) + 1]


def cli_scorer(fake_cli: dict, **options: object) -> object:
    environ = options.pop("environ", fake_cli["environ"])
    return tds.ClaudeCliScorer(STANDIN_PROMPTS, standin_record(), command=fake_cli["command"], environ=environ, timeout=options.pop("timeout", scaled_time_bound(CLI_CALL_S)), **options)


def test_the_cli_gets_the_pinned_model_the_system_prompt_the_rendered_prompt_and_nothing_else(fake_cli: dict) -> None:
    scorer = cli_scorer(fake_cli)

    tool_text = scorer.tool_answer(READ_THING, ["list_things", "ping"])
    coherence_text = scorer.coherence_answer("stats-api", STATS_TOOLS, tds.shadow_candidates(STATS_TOOLS))

    assert tool_text == coherence_text == fake_cli["environ"]["FAKE_CLAUDE_ANSWER"]
    assert scorer.calls == 2
    first, second = cli_calls(fake_cli["log"])
    for entry, system in ((first, STANDIN_TOOL_SYSTEM), (second, STANDIN_COHERENCE_SYSTEM)):
        argv = entry["argv"]
        assert "-p" in argv
        assert option(argv, "--model") == "stand-in-model"
        assert option(argv, "--system-prompt") == system
        assert option(argv, "--tools") == ""
        assert option(argv, "--setting-sources") == ""
        assert option(argv, "--output-format") == "json"
        for flag in ("--strict-mcp-config", "--safe-mode", "--disable-slash-commands", "--no-session-persistence"):
            assert flag in argv
        assert entry["listing"] == []
        assert not Path(entry["cwd"]).exists()
        assert not Path(entry["cwd"]).resolve().is_relative_to(ROOT)
        assert entry["env"]["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
        assert entry["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert first["cwd"] != second["cwd"]
    assert first["stdin"] == tds.render_tool_prompt(STANDIN_TOOL_USER, READ_THING, ["list_things", "ping"]) == READ_THING_PROMPT
    assert second["stdin"] == STATS_PROMPT


def test_the_cli_runs_no_more_calls_at_once_than_its_bound(fake_cli: dict) -> None:
    environ = {**fake_cli["environ"], "FAKE_CLAUDE_MODE": "slow"}
    scorer = cli_scorer(fake_cli, environ=environ, concurrency=2)
    threads = [threading.Thread(target=scorer.tool_answer, args=(tool(f"t{index}", "T."), [])) for index in range(5)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(scaled_time_bound(CLI_CALL_S))

    calls = cli_calls(fake_cli["log"])
    assert len(calls) == 5
    # The most calls running at one instant: at each call's start, every call
    # that has started and not yet ended. Counting every call one call overlaps
    # would also count a call that ended inside it and another that started
    # after that, two that never ran at the same time.
    at_once = max(sum(1 for other in calls if other["started"] <= entry["started"] < other["ended"]) for entry in calls)
    assert at_once <= 2, [(entry["started"], entry["ended"]) for entry in calls]


@pytest.mark.parametrize("mode", ["error", "wrong-model", "garbage", "nonzero"])
def test_a_failed_cli_call_is_a_backend_error(fake_cli: dict, mode: str) -> None:
    scorer = cli_scorer(fake_cli, environ={**fake_cli["environ"], "FAKE_CLAUDE_MODE": mode})

    with pytest.raises(tds.BackendError):
        scorer.tool_answer(READ_THING, [])


def test_a_cli_call_that_hangs_is_cut_off(fake_cli: dict) -> None:
    scorer = cli_scorer(fake_cli, environ={**fake_cli["environ"], "FAKE_CLAUDE_MODE": "hang"}, timeout=2.0)
    started = time.monotonic()

    with pytest.raises(tds.BackendError):
        scorer.tool_answer(READ_THING, [])

    assert time.monotonic() - started < scaled_time_bound(60)


def test_the_token_reaches_the_cli_through_its_environment_alone(fake_cli: dict) -> None:
    environ = {**fake_cli["environ"], TOKEN_VARIABLE: DUMMY_TOKEN, "ANTHROPIC_API_KEY": "an-api-key", "ANTHROPIC_AUTH_TOKEN": "an-auth-token"}
    scorer = cli_scorer(fake_cli, environ=environ, token_env=TOKEN_VARIABLE)

    scorer.tool_answer(READ_THING, [])
    scorer.tool_answer(READ_THING, [])

    calls = cli_calls(fake_cli["log"])
    for entry in calls:
        assert DUMMY_TOKEN not in json.dumps(entry["argv"])
        assert DUMMY_TOKEN not in entry["stdin"]
        assert entry["env"][TOKEN_VARIABLE] == DUMMY_TOKEN
        assert "ANTHROPIC_API_KEY" not in entry["env"]
        assert "ANTHROPIC_AUTH_TOKEN" not in entry["env"]
        assert entry["configListing"] == []
    assert calls[0]["env"]["CLAUDE_CONFIG_DIR"] != calls[1]["env"]["CLAUDE_CONFIG_DIR"]


def test_the_token_never_appears_in_an_error(fake_cli: dict) -> None:
    environ = {**fake_cli["environ"], TOKEN_VARIABLE: DUMMY_TOKEN, "FAKE_CLAUDE_MODE": "leak"}
    scorer = cli_scorer(fake_cli, environ=environ, token_env=TOKEN_VARIABLE)

    with pytest.raises(tds.BackendError) as raised:
        scorer.tool_answer(READ_THING, [])

    assert DUMMY_TOKEN not in str(raised.value)
    assert "refused the token" in str(raised.value)


def test_without_a_token_the_cli_keeps_the_developers_own_login(fake_cli: dict) -> None:
    environ = {name: value for name, value in fake_cli["environ"].items() if name not in (TOKEN_VARIABLE, "CLAUDE_CONFIG_DIR")}
    scorer = cli_scorer(fake_cli, environ=environ)

    scorer.tool_answer(READ_THING, [])

    (entry,) = cli_calls(fake_cli["log"])
    assert "CLAUDE_CONFIG_DIR" not in entry["env"]
    assert TOKEN_VARIABLE not in entry["env"]


@pytest.mark.parametrize(("installed", "warned"), [("0.0.0", False), ("9.9.9", True)])
def test_a_cli_version_other_than_the_recorded_one_is_reported(fake_cli: dict, installed: str, warned: bool) -> None:
    scorer = cli_scorer(fake_cli, environ={**fake_cli["environ"], "FAKE_CLAUDE_VERSION": installed})

    warnings = scorer.warnings()

    if warned:
        assert len(warnings) == 1
        assert "9.9.9" in warnings[0] and "0.0.0" in warnings[0]
    else:
        assert warnings == []
    assert scorer.calls == 0


# --- the entry point --------------------------------------------------------------


class MainRun:
    """main() with its seams replaced: the exports, the record, the prompts and the scorer."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, base_tools: list[dict], head_tools: list[dict], scorer: object = None) -> None:
        self.tmp_path = tmp_path
        self.base_export, self.head_export = export(base_tools), export(head_tools)
        self.scorer = scorer if scorer is not None else FakeScorer(default=tool_answer((4,) * 6))
        self.prompt_loads: list[object] = []
        self.scorers_built: list[object] = []
        self.prompt_error: Exception | None = None
        self.summary = tmp_path / "step-summary.md"
        self.summary.write_text("EARLIER SUMMARY\n", encoding="utf-8")
        self.output = tmp_path / "github-output"
        self.output.write_text("", encoding="utf-8")
        self.reports = tmp_path / "reports"
        monkeypatch.setattr(tds, "rev_parse", lambda repo, ref: "1" * 40 if ref != "HEAD" else "2" * 40)
        monkeypatch.setattr(tds, "export_revision", lambda repo, ref: copy.deepcopy(self.base_export))
        monkeypatch.setattr(tds, "export_tools", lambda src: copy.deepcopy(self.head_export))
        monkeypatch.setattr(tds, "load_version_record", standin_record)
        monkeypatch.setattr(tds, "load_prompts", self._load_prompts)
        monkeypatch.setattr(tds, "make_scorer", self._make_scorer)

    def _load_prompts(self, record: dict, cache_dir: object, fetch: object) -> dict:
        self.prompt_loads.append(cache_dir)
        if self.prompt_error is not None:
            raise self.prompt_error
        return dict(STANDIN_PROMPTS)

    def _make_scorer(self, prompts: dict, record: dict, args: object, environ: dict) -> object:
        self.scorers_built.append(prompts)
        return self.scorer

    def environ(self, token: bool = True) -> dict:
        environ = {"GITHUB_STEP_SUMMARY": str(self.summary), "GITHUB_OUTPUT": str(self.output)}
        if token:
            environ[TOKEN_VARIABLE] = DUMMY_TOKEN
        return environ

    def argv(self, *extra: str) -> list[str]:
        return ["--base", "origin/master", "--repo", str(self.tmp_path / "repo"), "--report-dir", str(self.reports), "--token-env", TOKEN_VARIABLE, *extra]

    def __call__(self, *extra: str, token: bool = True) -> int:
        return tds.main(self.argv(*extra), self.environ(token))


def lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return capsys.readouterr().out.splitlines()


def test_main_passes_unchanged_definitions_without_a_token_prompts_or_scorer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tools = [tool("alpha_tool", "Alpha.")]
    run = MainRun(monkeypatch, tmp_path, tools, tools)

    assert run(token=False) == 0

    assert any(line.startswith("PASS:") for line in lines(capsys))
    assert run.prompt_loads == []
    assert run.scorers_built == []
    assert "scored=false" in run.output.read_text(encoding="utf-8")


def test_main_refuses_changed_definitions_without_the_token_with_one_exact_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = MainRun(monkeypatch, tmp_path, [tool("alpha_tool", "Alpha.")], [tool("alpha_tool", "Alpha, reworded.")])

    assert run(token=False) == 2

    assert MISSING_TOKEN_LINE in lines(capsys)
    assert run.prompt_loads == []
    assert run.scorers_built == []
    assert "scored=false" in run.output.read_text(encoding="utf-8")
    assert json.loads((run.reports / "tool-definition-score.json").read_text(encoding="utf-8"))["decision"] == "invalid"


def test_main_passes_an_improvement_and_writes_the_reports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scorer = FakeScorer({"Alpha, before.": tool_answer((4,) * 6), "Alpha, after.": tool_answer((5,) * 6)})
    run = MainRun(monkeypatch, tmp_path, [tool("alpha_tool", "Alpha, before.")], [tool("alpha_tool", "Alpha, after.")], scorer)

    assert run() == 0

    assert any(line.startswith("PASS:") for line in lines(capsys))
    assert run.scorers_built == [STANDIN_PROMPTS]
    report = json.loads((run.reports / "tool-definition-score.json").read_text(encoding="utf-8"))
    assert report["decision"] == "pass"
    assert (report["baseCommit"], report["headCommit"]) == ("1" * 40, "2" * 40)
    markdown = (run.reports / "tool-definition-score.md").read_text(encoding="utf-8")
    assert markdown_row(markdown, "Overall", "4.0", "4.7")
    summary = run.summary.read_text(encoding="utf-8")
    assert summary.startswith("EARLIER SUMMARY\n")
    assert markdown.strip() in summary
    assert "scored=true" in run.output.read_text(encoding="utf-8")


def test_main_blocks_a_drop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scorer = FakeScorer({"Alpha, before.": tool_answer((5,) * 6), "Alpha, after.": tool_answer((4,) * 6)})
    run = MainRun(monkeypatch, tmp_path, [tool("alpha_tool", "Alpha, before.")], [tool("alpha_tool", "Alpha, after.")], scorer)

    assert run() == 1

    assert any(line.startswith("BLOCK:") for line in lines(capsys))


def test_main_calls_an_answer_that_stays_invalid_invalid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scorer = FakeScorer({"Alpha, before.": tool_answer((4,) * 6), "Alpha, after.": "this is not json"})
    run = MainRun(monkeypatch, tmp_path, [tool("alpha_tool", "Alpha, before.")], [tool("alpha_tool", "Alpha, after.")], scorer)

    assert run() == 2

    assert any(line.startswith("INVALID:") and "alpha_tool" in line for line in lines(capsys))


def test_main_refuses_mismatched_prompts_before_any_scoring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = MainRun(monkeypatch, tmp_path, [tool("alpha_tool", "Alpha.")], [tool("alpha_tool", "Alpha, reworded.")])
    run.prompt_error = tds.PromptMismatch("the fetched tool_user prompt does not match its recorded sha256")

    assert run() == 2

    assert any(line.startswith("INVALID:") and "tool_user" in line for line in lines(capsys))
    assert run.scorers_built == []
    assert run.scorer.tool_calls == []  # type: ignore[attr-defined]


def test_main_turns_an_unexpected_failure_into_an_invalid_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = MainRun(monkeypatch, tmp_path, [tool("alpha_tool", "Alpha.")], [tool("alpha_tool", "Alpha.")])

    def broken(repo: object, ref: object) -> dict:
        raise RuntimeError("the base worktree could not be created")

    monkeypatch.setattr(tds, "export_revision", broken)

    assert run() == 2

    assert any(line.startswith("INVALID:") and "the base worktree could not be created" in line for line in lines(capsys))


def test_main_check_accepts_a_current_report_and_rejects_a_stale_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scorer = FakeScorer({"Alpha, before.": tool_answer((4,) * 6), "Alpha, after.": tool_answer((5,) * 6)})
    run = MainRun(monkeypatch, tmp_path, [tool("alpha_tool", "Alpha, before.")], [tool("alpha_tool", "Alpha, after.")], scorer)
    assert run() == 0
    saved = tmp_path / "saved.json"
    shutil.copyfile(run.reports / "tool-definition-score.json", saved)
    capsys.readouterr()

    assert run("--check", str(saved)) == 0
    run.head_export = export([tool("alpha_tool", "Alpha, changed again.")])
    assert run("--check", str(saved)) == 2

    assert any(line.startswith("INVALID:") and "stale" in line.lower() for line in lines(capsys))


def test_the_report_directory_is_ignored_by_git() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".tool-definition-score/" in ignored


# --- the CI job -----------------------------------------------------------------


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def score_job() -> dict:
    return workflow()["jobs"]["tool_definition_score"]


def step_by_id(job: dict, step_id: str) -> dict:
    return next(step for step in job["steps"] if step.get("id") == step_id)


def test_the_score_job_runs_on_pull_requests_against_their_base() -> None:
    job = score_job()

    assert "github.event_name == 'pull_request'" in job["if"]
    checkout = next(step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["persist-credentials"] is False
    score = step_by_id(job, "score")
    assert 'python tools/tool_definition_score.py --base "origin/${{ github.base_ref }}" --token-env CLAUDE_CODE_OAUTH_TOKEN' in score["run"]
    assert score["env"][TOKEN_VARIABLE] == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"


def test_the_score_job_pins_its_actions_and_its_cli() -> None:
    job = score_job()

    for step in job["steps"]:
        if "uses" in step:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"].split(" ")[0]), step["uses"]
    install = next(step for step in job["steps"] if step.get("run", "").strip().startswith("npm ci"))
    assert install["working-directory"] == "tools/tdqs"
    record = json.loads((TDQS_DIRECTORY / "version.json").read_text(encoding="utf-8"))
    package = json.loads((TDQS_DIRECTORY / "package.json").read_text(encoding="utf-8"))
    assert package["dependencies"] == {"@anthropic-ai/claude-code": record["cli_version"]}
    locked = json.loads((TDQS_DIRECTORY / "package-lock.json").read_text(encoding="utf-8"))["packages"]["node_modules/@anthropic-ai/claude-code"]
    assert locked["version"] == record["cli_version"]
    assert locked["integrity"].startswith("sha512-")


def test_the_score_job_keeps_the_secret_to_the_steps_that_call_the_model() -> None:
    job = score_job()

    assert "secrets." not in json.dumps({key: value for key, value in job.items() if key != "steps"})
    for step in job["steps"]:
        if step.get("id") in ("score", "model"):
            continue
        assert "secrets." not in json.dumps(step), step


def test_the_score_job_uploads_its_report_whatever_happened() -> None:
    upload = next(step for step in score_job()["steps"] if str(step.get("uses", "")).startswith("actions/upload-artifact@"))

    assert upload["if"] in ("always()", "${{ always() }}")
    assert ".tool-definition-score" in upload["with"]["path"]


def test_the_score_job_runs_the_model_test_when_it_scored() -> None:
    model = step_by_id(score_job(), "model")

    assert "steps.score.outputs.scored == 'true'" in model["if"]
    assert model["env"]["AGENTIC_HIL_TDQS_MODEL"] == "1"
    assert model["env"][TOKEN_VARIABLE] == "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"
    assert "tests/test_tool_definition_score_model.py" in model["run"]


def test_required_ci_needs_the_score_job_and_lets_it_skip_only_off_pull_requests() -> None:
    required = workflow()["jobs"]["required-ci"]

    assert "tool_definition_score" in required["needs"]
    checks = "\n".join(step.get("run", "") for step in required["steps"])
    guard = (
        r'if \[\[ "\$\{\{ needs\.tool_definition_score\.result \}\}" != "success" \]\] && '
        r'! \[\[ "\$\{\{ needs\.tool_definition_score\.result \}\}" == "skipped" && "\$\{\{ github\.event_name \}\}" != "pull_request" \]\]; then\n'
        r'\s*echo "[^"\n]*\$\{\{ needs\.tool_definition_score\.result \}\}"\n'
        r"\s*exit 1\n"
        r"\s*fi"
    )
    assert re.search(guard, checks), checks


# --- the export: what a host sees over stdio ----------------------------------


def provisioned_listing(workspace: Path) -> list[dict]:
    """tools/list answered in process by a provisioned server, the way test_mcp_envelope.py builds one."""
    service = AgenticHILToolService(load_config(str(write_config(workspace))), frontend="mcp")
    response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, service)
    assert isinstance(response, dict)
    return json.loads(json.dumps(response["result"]["tools"]))


class LaunchRecorder:
    """Stands in front of the real server launch, records what each launch was given and
    answered, and lets `observe` look around while the child is about to run."""

    def __init__(self, real: Callable, observe: Callable[[dict], object] | None = None) -> None:
        self.real = real
        self.observe = observe
        self.launches: list[dict] = []

    def __call__(self, argv: list[str], cwd: object, env: dict, stdin_text: str, timeout: float) -> tuple[int, str, str]:
        launch: dict = {"argv": list(argv), "cwd": Path(cwd), "listing": sorted(os.listdir(cwd)), "env": dict(env), "stdin": stdin_text}  # type: ignore[arg-type]
        if self.observe is not None:
            launch["observed"] = self.observe(launch)
        returncode, stdout, stderr = self.real(argv, cwd, env, stdin_text, timeout)
        launch["stdout"] = stdout
        self.launches.append(launch)
        return returncode, stdout, stderr


def assert_a_host_handshake(launch: dict) -> None:
    """Three messages, as a host sends them, from an empty directory outside this checkout,
    with no Agentic HIL configuration in the environment and no user site."""
    messages = [json.loads(line) for line in launch["stdin"].splitlines() if line.strip()]
    assert [message["method"] for message in messages] == ["initialize", "notifications/initialized", "tools/list"]
    assert "id" not in messages[1]
    assert launch["listing"] == []
    assert not launch["cwd"].resolve().is_relative_to(ROOT)
    assert not [name for name in launch["env"] if name.upper().startswith("AGENTIC_HIL_")]
    assert launch["env"]["PYTHONNOUSERSITE"] == "1"


def listed_tools(launch: dict) -> list[dict]:
    responses = [json.loads(line) for line in launch["stdout"].splitlines() if line.strip()]
    (listing,) = [response for response in responses if response.get("id") == 2]
    return listing["result"]["tools"]


def test_the_export_lists_what_a_provisioned_server_lists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The export starts the server with no configuration, as the registry does, over
    stdio, and gets the same tools a provisioned server lists."""
    recorder = LaunchRecorder(tds.run_server)
    monkeypatch.setattr(tds, "run_server", recorder)

    exported = tds.export_tools(SRC)

    (launch,) = recorder.launches
    assert_a_host_handshake(launch)
    assert Path(launch["argv"][-1]).resolve() == SRC.resolve()
    assert exported["tools"] == listed_tools(launch)
    assert exported["serverName"] == "agentic-hil"
    assert exported["serverVersion"] == __version__
    assert [item["name"] for item in exported["tools"]] == MCP_TOOL_NAMES
    assert exported["tools"] == provisioned_listing(tmp_path / "workspace")
    assert exported["hashes"] == {item["name"]: tds.definition_hash(item) for item in exported["tools"]}
    assert exported["setHash"] == tds.set_hash(exported["tools"])


def test_the_export_ignores_a_configuration_bound_to_another_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AGENTIC_HIL_CONFIG pointing at another project would end the server with config_invalid."""
    write_authoritative_config(tmp_path / "elsewhere", monkeypatch)
    assert os.environ.get("AGENTIC_HIL_CONFIG")
    recorder = LaunchRecorder(tds.run_server)
    monkeypatch.setattr(tds, "run_server", recorder)

    exported = tds.export_tools(SRC)

    assert [item["name"] for item in exported["tools"]] == MCP_TOOL_NAMES
    (launch,) = recorder.launches
    assert "AGENTIC_HIL_CONFIG" not in launch["env"]


def git(where: Path, *args: str) -> str:
    """Run git in `where` with none of this process's own GIT_ variables, and fail naming its error."""
    clean = {name: value for name, value in os.environ.items() if not name.upper().startswith("GIT_")}
    done = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
        cwd=where,
        env=clean,
        capture_output=True,
        text=True,
        timeout=scaled_time_bound(GIT_CALL_S),
        check=False,
    )
    assert done.returncode == 0, f"git {' '.join(args)} failed in {where}: {done.stderr.strip()}"
    return done.stdout


def worktrees(repo: Path) -> list[Path]:
    return [Path(line.split(" ", 1)[1]).resolve() for line in git(repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]


def test_a_revision_and_a_changed_working_tree_are_told_apart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The base comes from a git worktree of the ref, exported with that tree's own src;
    the head is the working tree. In the working tree of a test copy a tool is added,
    one is changed and one removed."""
    repo = tmp_path / "repo"
    shutil.copytree(SRC / "agentic_hil", repo / "src" / "agentic_hil", ignore=shutil.ignore_patterns("__pycache__"))
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    first, last = MCP_TOOL_NAMES[0], MCP_TOOL_NAMES[-1]
    contracts = repo / "src" / "agentic_hil" / "contracts.py"
    appended = [
        "",
        f"MCP_TOOLS[:] = [item for item in MCP_TOOLS if item['name'] != {last!r}]",
        f"next(item for item in MCP_TOOLS if item['name'] == {first!r})['description'] += ' Changed in a test copy.'",
        "MCP_TOOLS.append({'name': 'tdqs_added_tool', 'description': 'Exists only in a test copy.', "
        "'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}})",
        "",
    ]
    with contracts.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(appended))

    def observe(launch: dict) -> dict:
        src = Path(launch["argv"][-1]).resolve()
        return {"src": src, "worktrees": worktrees(repo), "contracts": (src / "agentic_hil" / "contracts.py").read_text(encoding="utf-8")}

    recorder = LaunchRecorder(tds.run_server, observe)
    monkeypatch.setattr(tds, "run_server", recorder)

    base = tds.export_revision(repo, "HEAD")
    head = tds.export_tools(repo / "src")

    base_launch, head_launch = recorder.launches
    for launch in (base_launch, head_launch):
        assert_a_host_handshake(launch)
    seen = base_launch["observed"]
    assert seen["src"].parent in seen["worktrees"]
    assert seen["src"].parent != repo.resolve()
    assert "tdqs_added_tool" not in seen["contracts"]
    assert head_launch["observed"]["src"] == (repo / "src").resolve()
    assert "tdqs_added_tool" in head_launch["observed"]["contracts"]
    assert base["tools"] == listed_tools(base_launch)
    assert base["tools"] == provisioned_listing(tmp_path / "workspace")
    diff = tds.diff_definitions(base["tools"], head["tools"])
    assert list(diff.added) == ["tdqs_added_tool"]
    assert list(diff.removed) == [last]
    assert list(diff.changed) == [first]
    assert base["setHash"] != head["setHash"]
    assert worktrees(repo) == [repo.resolve()]
    assert git(repo, "status", "--porcelain").split() == ["M", "src/agentic_hil/contracts.py"]
