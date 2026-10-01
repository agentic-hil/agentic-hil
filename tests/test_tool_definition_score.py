"""The tool-definition score gate, held to the TDQS specification v1.3.

`tools/tool_definition_score.py` keeps the server's overall Tool Definition
Quality Score from falling: a change passes when the overall score after it is
at least the overall score before it, compared on the number, not the letter
tier. Everything in this file runs without a model and without the network.
The scorer is a fake that hands back canned answers, and the prompt texts are
stand-ins, because the upstream specification carries no license and its
prompts are fetched at run time and never committed.

The section names in the comments are those of the specification README at the
pinned upstream commit b9881b0cfec8: Stage 1 (context signals, invocation
cost), Stage 2 (hard gates), the LLM output contract, Stage 4 (post-processing),
Computing the score, Tiers, Server-level scores, Shadowed tools, Overall, Output
format, and Running TDQS at scale (the four referential checks on shadowing
risks). The export tests start the real server over stdio from this checkout,
the way a host does.
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
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

import pytest
from conftest import write_authoritative_config, write_config
from support import scaled_time_bound

from agentic_hil import __version__
from agentic_hil.config import load_config
from agentic_hil.contracts import MCP_TOOL_NAMES
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

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
GIT_CALL_S = 60


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

    `table` maps a tool's description to the answer for it. `coherence` is the
    answer for every set, or a callable that receives the tool list. Calls are
    recorded under a lock because the gate may score concurrently."""

    def __init__(self, table: dict | None = None, coherence: object = None) -> None:
        self.table = table or {}
        self.coherence = coherence if coherence is not None else coherence_answer((4, 4, 4, 4))
        self.lock = threading.Lock()
        self.tool_calls: list[tuple[str, tuple[str, ...]]] = []
        self.coherence_calls: list[tuple[str, tuple[str, ...], list]] = []

    def tool_answer(self, definition: dict, sibling_names: object) -> str:
        with self.lock:
            self.tool_calls.append((definition["name"], tuple(sibling_names)))  # type: ignore[arg-type]
        return self.table[definition.get("description")]

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
        "confirmation": {"version": 1, "pairs": 3},
    }
    record.update(changes)
    return record


def export(tools: list[dict], version: str = "1.0") -> dict:
    return tds.export_from_tools(copy.deepcopy(tools), server_name="agentic-hil", server_version=version)


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
    results = {"a": {"tdqs": Fraction("2.6")}, "b": {"tdqs": Fraction("2.9")}, "c": {"tdqs": Fraction("2.9")}, "d": {"tdqs": Fraction("5.0")}}

    rollups = tds.rollups(results, ["a", "b", "c", "d"], {"coherenceScore": Fraction("3.3")})

    assert rollups["toolCount"] == 4
    assert rollups["scoredToolCount"] == 4
    assert rollups["minTdqs"] == Fraction("2.6")
    assert rollups["minTool"] == "a"
    assert rollups["descriptionQualityScore"] == Fraction("3.1")
    assert rollups["descriptionQualityTier"] == "B"
    assert rollups["coherenceScore"] == Fraction("3.3")
    assert rollups["coherenceTier"] == "B"
    assert rollups["overallScore"] == Fraction("3.2")
    assert rollups["overallTier"] == "B"


def test_rollups_refuse_an_unscored_tool() -> None:
    """The registry rolls up at 80 % coverage; the gate needs every tool scored."""
    assert issubclass(tds.IncompleteResult, tds.InvalidComparison)
    results = {"a": {"tdqs": Fraction("3.0")}, "b": {"tdqs": Fraction("4.0")}}

    with pytest.raises(tds.IncompleteResult):
        tds.rollups(results, ["a", "b", "c"], {"coherenceScore": Fraction("4.0")})


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


def test_annotation_values_come_from_the_hints() -> None:
    signals = tds.context_signals(tool("read_thing", annotations={"title": "Read a thing", "readOnlyHint": True, "openWorldHint": False}))

    assert signals["hasAnnotations"] is True
    assert signals["annotationValues"] == {"readOnly": True, "destructive": None, "idempotent": None, "openWorld": False}
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


def test_a_recursive_schema_terminates() -> None:
    recursive = {
        "type": "object",
        "properties": {"node": {"$ref": "#/$defs/Node"}},
        "required": ["node"],
        "$defs": {"Node": {"type": "object", "properties": {"value": {"type": "string"}, "next": {"$ref": "#/$defs/Node"}}, "required": ["value", "next"]}},
    }

    count, depth, unions, cost = cost_signals(recursive)

    assert 1 <= depth <= 10
    assert cost == count + 2 * max(0, depth - 1) + 2 * unions


def test_depth_is_capped_at_ten() -> None:
    schema: dict = scalars("leaf")
    for _ in range(14):
        schema = {"type": "object", "properties": {"child": schema}, "required": ["child"]}

    count, depth, unions, cost = cost_signals(schema)

    assert depth == 10
    assert cost == count + 2 * 9 + 2 * unions


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


def test_export_from_tools_carries_the_definitions_and_their_hashes() -> None:
    tools = [tool("a", "A."), HASHED]

    exported = tds.export_from_tools(copy.deepcopy(tools), server_name="agentic-hil", server_version="1.0")

    assert exported["serverName"] == "agentic-hil"
    assert exported["serverVersion"] == "1.0"
    assert exported["tools"] == tools
    assert exported["hashes"] == {item["name"]: tds.definition_hash(item) for item in tools}
    assert exported["setHash"] == tds.set_hash(tools)


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
    assert result["tdqs"] == Fraction("1.0")
    assert result["tier"] == "D"
    assert result["flags"] == ["No Description"]


def test_a_tautological_description_caps_purpose_clarity_at_two() -> None:
    """The description lowercased and trimmed is the name. The model is still asked; its 5 becomes 2."""
    scorer = FakeScorer({"  Process\n": tool_answer((5, 5, 5, 5, 5, 5))})

    result = tds.evaluate_tool(tool("process", "  Process\n"), [], scorer)

    assert len(scorer.tool_calls) == 1
    assert result["scores"]["purpose_clarity"] == 2
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
    definition = tool("create_record", "Creates a new record.", annotations={"readOnlyHint": True})
    scorer = FakeScorer({"Creates a new record.": tool_answer((4, 2, 2, 3, 4, 2), contradiction=True)})

    result = tds.evaluate_tool(definition, ["get_record"], scorer)

    assert result["flags"] == ["Annotation Contradiction"]
    assert result["smells"] == ["usage_guidelines", "behavioral_transparency", "contextual_completeness"]
    assert result["scores"] == scores(4, 2, 2, 3, 4, 2)
    assert result["tdqs"] == Fraction("2.9")
    assert result["tier"] == "C"
    assert result["justifications"]["usage_guidelines"] == {"score": 2, "justification": "usage_guidelines justification"}
    assert set(result["justifications"]) == set(DIMENSIONS)
    assert result["summary"] == "A summary."
    assert result["serverFlags"] == []
    assert result["definitionHash"] == tds.definition_hash(definition)
    assert result["contextSignals"] == tds.context_signals(definition)


# --- the output contract and the retry limit (LLM output contract) ------------

GOOD_TOOL_ANSWER = json.loads(tool_answer((4, 2, 2, 3, 4, 2)))
GOOD_COHERENCE_ANSWER = json.loads(coherence_answer((4, 3, 3, 3)))


def changed_answer(answer: dict, change: Callable[[dict], object]) -> str:
    edited = copy.deepcopy(answer)
    change(edited)
    return json.dumps(edited)


def set_score(value: object) -> Callable[[dict], object]:
    return lambda answer: answer["scores"]["purpose_clarity"].__setitem__("score", value)


BAD_TOOL_ANSWERS = [
    pytest.param("this is not json", id="not-json"),
    pytest.param("[]", id="a-list"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, lambda a: a.__setitem__("scores", [])), id="scores-not-an-object"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, lambda a: a["scores"].pop("parameter_semantics")), id="a-dimension-missing"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, lambda a: a["scores"].__setitem__("purpose_clarity", 4)), id="a-dimension-not-an-object"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, set_score(0)), id="score-0"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, set_score(6)), id="score-6"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, set_score(3.5)), id="score-3.5"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, set_score("4")), id="score-a-string"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, set_score(True)), id="score-a-boolean"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, lambda a: a["scores"]["usage_guidelines"].pop("justification")), id="justification-missing"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, lambda a: a["scores"]["usage_guidelines"].__setitem__("justification", 5)), id="justification-not-a-string"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, lambda a: a.pop("annotation_contradiction")), id="contradiction-missing"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, lambda a: a.__setitem__("annotation_contradiction", "false")), id="contradiction-a-string"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, lambda a: a.pop("summary")), id="summary-missing"),
    pytest.param(changed_answer(GOOD_TOOL_ANSWER, lambda a: a.__setitem__("summary", 3)), id="summary-not-a-string"),
]


def test_a_good_tool_answer_parses() -> None:
    parsed = tds.parse_tool_answer(json.dumps(GOOD_TOOL_ANSWER))

    assert {dimension: parsed["scores"][dimension]["score"] for dimension in DIMENSIONS} == scores(4, 2, 2, 3, 4, 2)
    assert parsed["annotation_contradiction"] is False


@pytest.mark.parametrize("text", BAD_TOOL_ANSWERS)
def test_a_bad_tool_answer_is_refused(text: str) -> None:
    with pytest.raises(tds.InvalidAnswer):
        tds.parse_tool_answer(text)


BAD_COHERENCE_ANSWERS = [
    pytest.param("not json either", id="not-json"),
    pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, lambda a: a["scores"].pop("completeness")), id="a-dimension-missing"),
    pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, lambda a: a["scores"]["completeness"].__setitem__("score", 6)), id="score-out-of-range"),
    pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, lambda a: a["scores"]["completeness"].__setitem__("score", 2.5)), id="score-not-an-integer"),
    pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, lambda a: a["scores"]["disambiguation"].pop("justification")), id="justification-missing"),
    pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, lambda a: a.__setitem__("shadowing_risks", {})), id="risks-not-a-list"),
    pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, lambda a: a.__setitem__("shadowing_risks", [{"tool": "a", "cheaper_sibling": "b"}])), id="a-risk-without-justification"),
    pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, lambda a: a.__setitem__("shadowing_risks", [{"tool": 1, "cheaper_sibling": "b", "justification": "j"}])), id="a-risk-with-a-non-string-name"),
    pytest.param(changed_answer(GOOD_COHERENCE_ANSWER, lambda a: a.pop("summary")), id="summary-missing"),
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

    with pytest.raises(tds.InvalidComparison):
        tds.evaluate_tool(tool("t", "Does t."), [], scorer)

    assert scorer.tool_calls == 1 + tds.RETRIES


def test_a_backend_failure_is_retried_like_an_invalid_answer() -> None:
    scorer = SequenceScorer(tool_answers=[tds.BackendError("the model call failed"), tool_answer((4, 4, 4, 4, 4, 4))])

    result = tds.evaluate_tool(tool("t", "Does t."), [], scorer)

    assert scorer.tool_calls == 2
    assert result["tdqs"] == Fraction("4.0")


def test_a_coherence_answer_still_invalid_after_the_retries_makes_the_comparison_invalid() -> None:
    scorer = SequenceScorer(coherence_answers=["this is not json"])

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

    assert {item["tool"]: item["cheaperSibling"] for item in candidates} == {"query_panel": "get_stat", "lookup": "ping", "get_stat": "ping"}
    assert len(candidates) == 3
    query_panel = next(item for item in candidates if item["tool"] == "query_panel")
    assert (query_panel["invocationCost"], query_panel["cheaperSiblingInvocationCost"]) == (13, 4)


def test_the_prefilter_does_not_depend_on_tool_order() -> None:
    forward = {item["tool"]: item["cheaperSibling"] for item in tds.shadow_candidates(STATS_TOOLS)}
    backward = {item["tool"]: item["cheaperSibling"] for item in tds.shadow_candidates(list(reversed(STATS_TOOLS)))}

    assert backward == forward


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

    assert [item["tool"] for item in confirmed].count("query_panel") <= 1


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

    side = tds.score_side(tds.export_from_tools(copy.deepcopy(STATS_TOOLS), server_name="stats-api", server_version="1"), scorer)

    assert sorted(side["tools"]) == sorted(names)
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


def side(overall: str) -> dict:
    return {"rollups": {"overallScore": Fraction(overall)}}


class PairRunner:
    """Hands out the given (base, head) overall scores one pair per call, and counts the calls."""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self.pairs = list(pairs)
        self.calls = 0

    def __call__(self) -> tuple[dict, dict]:
        base, head = self.pairs[self.calls]
        self.calls += 1
        return side(base), side(head)


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


def test_an_invalid_pair_during_confirmation_never_passes() -> None:
    calls = []

    def run_pair() -> tuple[dict, dict]:
        calls.append(1)
        if len(calls) == 1:
            return side("3.7"), side("3.6")
        raise tds.InvalidComparison("the second pair could not be scored")

    with pytest.raises(tds.InvalidComparison):
        tds.confirm(run_pair)


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
    assert "unchanged" in report["reason"].lower() or "identical" in report["reason"].lower()


def test_unchanged_definitions_pass_without_a_scorer_at_all() -> None:
    """No credential is needed when nothing changed: the scorer is never built."""
    tools = [tool("alpha_tool", "Alpha.")]

    report = tds.compare(export(tools), export(tools), None, standin_record())

    assert report["decision"] == "pass"
    assert report["exitCode"] == 0


def exact_mean(scored_side: dict) -> Fraction:
    values = [result["tdqs"] for result in scored_side["tools"].values()]
    return sum(values, Fraction(0)) / len(values)


def test_a_new_tool_that_lowers_the_overall_only_through_the_minimum_term_blocks() -> None:
    """Base: 3.5 and 3.6, coherence 4.0, overall 3.7. Head: both rise to 4.5 and a new
    tool at 2.5 joins; the mean rises (3.55 to 3.83) but the minimum falls to 2.5,
    description quality falls to 3.3, and the overall to 3.5."""
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

    assert report["decision"] == "block"
    assert report["exitCode"] == 1
    assert len(report["pairs"]) == 3
    assert len(scorer.tool_calls) == 3 * (2 + 3)
    assert len(scorer.coherence_calls) == 3 * 2
    first = report["pairs"][0]
    assert first["base"]["rollups"]["overallScore"] == Fraction("3.7")
    assert first["head"]["rollups"]["overallScore"] == Fraction("3.5")
    assert first["head"]["rollups"]["descriptionQualityScore"] == Fraction("3.3")
    assert exact_mean(first["head"]) > exact_mean(first["base"])
    assert report["causes"]["minimumTerm"]["head"]["tool"] == "new_tool"
    summary = tds.summary_markdown(report)
    assert "new_tool" in summary
    assert "minimum" in summary.lower()
    assert "3.7" in summary
    assert "3.5" in summary


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


def test_the_tools_that_fell_are_sorted_by_their_effect_on_the_overall() -> None:
    """zeta_tool falls 4.5 to 2.5 and becomes the minimum; beta_tool falls 4.5 to 4.3."""
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
    assert [item["tool"] for item in report["causes"]["tools"]] == ["zeta_tool", "beta_tool"]
    assert report["causes"]["minimumTerm"]["head"]["tool"] == "zeta_tool"


def test_an_answer_that_stays_invalid_makes_the_comparison_invalid() -> None:
    base_tools = [tool("broken_tool", "Broken, before."), tool("fine_tool", "Fine.")]
    head_tools = [tool("broken_tool", "Broken, after."), tool("fine_tool", "Fine.")]
    scorer = FakeScorer(
        {
            "Broken, before.": tool_answer((4, 4, 4, 4, 4, 4)),
            "Broken, after.": "this is not json",
            "Fine.": tool_answer((4, 4, 4, 4, 4, 4)),
        }
    )

    report = tds.compare(export(base_tools), export(head_tools), scorer, standin_record())

    assert report["decision"] == "invalid"
    assert report["exitCode"] == 2
    assert "broken_tool" in report["reason"]


def test_the_report_carries_exports_versions_and_commits_and_serializes() -> None:
    base_tools = [tool("alpha_tool", "Alpha, before.")]
    head_tools = [tool("alpha_tool", "Alpha, after.")]
    scorer = FakeScorer({"Alpha, before.": tool_answer((4, 4, 4, 4, 4, 4)), "Alpha, after.": tool_answer((5, 5, 5, 5, 5, 5))})
    base_export, head_export = export(base_tools), export(head_tools)
    record = standin_record()

    report = tds.compare(base_export, head_export, scorer, record, base_commit="1" * 40, head_commit="2" * 40)

    assert report["decision"] == "pass"
    assert len(report["pairs"]) == 1
    assert report["base"]["export"]["setHash"] == base_export["setHash"]
    assert report["head"]["export"]["hashes"] == head_export["hashes"]
    assert (report["baseCommit"], report["headCommit"]) == ("1" * 40, "2" * 40)
    assert report["versions"]["model"] == record["model"]
    parsed = json.loads(tds.report_json(report))
    assert parsed["pairs"][0]["head"]["rollups"]["overallScore"] == 4.7
    assert parsed["pairs"][0]["base"]["tools"]["alpha_tool"]["tdqs"] == 4.0


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


# --- the version record and the calibration record ----------------------------


def test_the_committed_version_record_pins_model_spec_prompts_and_rubric() -> None:
    record = tds.load_version_record()

    assert record["model"] == "claude-haiku-4-5-20251001"
    assert re.fullmatch(r"\d+\.\d+\.\d+", record["cli_version"])
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
    assert isinstance(record["confirmation"]["version"], int)


def test_the_calibration_record_belongs_to_this_version_record() -> None:
    """A change to anything in the version record needs a new calibration."""
    assert tds.load_calibration_record()["version_digest"] == tds.version_digest(tds.load_version_record())


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda r: r.__setitem__("model", "another-model"), id="model"),
        pytest.param(lambda r: r.__setitem__("cli_version", "9.9.9"), id="cli-version"),
        pytest.param(lambda r: r.__setitem__("spec_commit", "f" * 40), id="spec-commit"),
        pytest.param(lambda r: r.__setitem__("spec_version", "1.4"), id="spec-version"),
        pytest.param(lambda r: r["prompt_sha256"].__setitem__("coherence_user", "0" * 64), id="a-prompt-hash"),
        pytest.param(lambda r: r["dimension_weights"].__setitem__("purpose_clarity", 30), id="a-weight"),
        pytest.param(lambda r: r["overall_weights"].__setitem__("coherence", 31), id="a-rollup-weight"),
        pytest.param(lambda r: r["tier_thresholds"].__setitem__("A", 3.6), id="a-tier-threshold"),
        pytest.param(lambda r: r.__setitem__("retries", 3), id="the-retry-count"),
        pytest.param(lambda r: r["confirmation"].__setitem__("version", 2), id="the-confirmation-version"),
    ],
)
def test_the_version_digest_moves_with_every_field(change: Callable[[dict], object]) -> None:
    record = standin_record()
    changed = copy.deepcopy(record)
    change(changed)

    assert re.fullmatch(r"[0-9a-f]{64}", tds.version_digest(record))
    assert tds.version_digest(reversed_keys(record)) == tds.version_digest(record)  # type: ignore[arg-type]
    assert tds.version_digest(changed) != tds.version_digest(record)


# --- the prompts: fetched, extracted, verified, never committed ---------------

STANDIN_README = "\n".join(
    [
        "# Stand-in specification",
        "",
        "Prose before the appendices, with a block that is not a prompt:",
        "",
        "```text",
        "DECOY BLOCK, NOT A PROMPT",
        "```",
        "",
        "## Appendix A: Tool scoring prompt",
        "",
        "The system prompt, verbatim:",
        "",
        "```text",
        "STAND-IN TOOL SYSTEM PROMPT",
        "",
        "## A heading inside the fence",
        "Score {name} on six dimensions.",
        "```",
        "",
        "The user message template:",
        "",
        "```text",
        "STAND-IN TOOL USER TEMPLATE for {name}",
        "```",
        "",
        "## Appendix B: Server coherence prompt",
        "",
        "```text",
        "STAND-IN COHERENCE SYSTEM PROMPT",
        "## Another heading inside the fence",
        "```",
        "",
        "```text",
        "STAND-IN COHERENCE USER TEMPLATE for {serverName}",
        "```",
        "",
        "## References",
        "",
    ]
)
STANDIN_PROMPTS = {
    "tool_system": "STAND-IN TOOL SYSTEM PROMPT\n\n## A heading inside the fence\nScore {name} on six dimensions.",
    "tool_user": "STAND-IN TOOL USER TEMPLATE for {name}",
    "coherence_system": "STAND-IN COHERENCE SYSTEM PROMPT\n## Another heading inside the fence",
    "coherence_user": "STAND-IN COHERENCE USER TEMPLATE for {serverName}",
}
STANDIN_HASHES = {key: hashlib.sha256(text.encode("utf-8")).hexdigest() for key, text in STANDIN_PROMPTS.items()}
STANDIN_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def test_the_readme_is_fetched_at_the_full_commit() -> None:
    assert tds.upstream_readme_url(STANDIN_COMMIT) == f"https://raw.githubusercontent.com/glama-ai/tool-definition-quality-score/{STANDIN_COMMIT}/README.md"


def test_the_four_prompts_are_extracted_from_the_appendices() -> None:
    assert tds.extract_prompts(STANDIN_README) == STANDIN_PROMPTS


def test_a_readme_without_an_appendix_is_refused() -> None:
    truncated = STANDIN_README.split("## Appendix B")[0]

    with pytest.raises(tds.PromptMismatch):
        tds.extract_prompts(truncated)


def test_prompts_that_match_their_hashes_are_accepted_and_a_changed_one_is_named() -> None:
    tds.verify_prompts(STANDIN_PROMPTS, STANDIN_HASHES)
    changed = {**STANDIN_PROMPTS, "coherence_user": STANDIN_PROMPTS["coherence_user"] + " "}

    with pytest.raises(tds.PromptMismatch, match="coherence_user"):
        tds.verify_prompts(changed, STANDIN_HASHES)


def test_load_prompts_fetches_once_and_then_reads_the_cache(tmp_path: Path) -> None:
    record = {"spec_commit": STANDIN_COMMIT, "prompt_sha256": STANDIN_HASHES}
    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return STANDIN_README

    def no_network(url: str) -> str:
        raise AssertionError(f"fetched {url} although the cache holds it")

    assert tds.load_prompts(record, tmp_path / "cache", fetch) == STANDIN_PROMPTS
    assert fetched == [tds.upstream_readme_url(STANDIN_COMMIT)]
    assert tds.load_prompts(record, tmp_path / "cache", no_network) == STANDIN_PROMPTS


def test_load_prompts_refuses_a_mismatch_and_does_not_keep_it(tmp_path: Path) -> None:
    record = {"spec_commit": STANDIN_COMMIT, "prompt_sha256": STANDIN_HASHES}
    altered = STANDIN_README.replace("STAND-IN TOOL SYSTEM PROMPT", "A DIFFERENT SYSTEM PROMPT")
    fetched: list[str] = []

    with pytest.raises(tds.PromptMismatch):
        tds.load_prompts(record, tmp_path / "cache", lambda url: altered)

    assert tds.load_prompts(record, tmp_path / "cache", lambda url: fetched.append(url) or STANDIN_README) == STANDIN_PROMPTS
    assert len(fetched) == 1


def test_the_default_cache_is_outside_the_repository() -> None:
    assert not tds.default_cache_dir().resolve().is_relative_to(ROOT)


# --- the export: what a host sees over stdio ----------------------------------


def provisioned_listing(workspace: Path) -> list[dict]:
    """tools/list answered in process by a provisioned server, the way test_mcp_envelope.py builds one."""
    service = AgenticHILToolService(load_config(str(write_config(workspace))), frontend="mcp")
    response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, service)
    assert isinstance(response, dict)
    return json.loads(json.dumps(response["result"]["tools"]))


def test_the_export_lists_what_a_provisioned_server_lists(tmp_path: Path) -> None:
    """The export starts the server with no configuration, as the registry does,
    and gets the same tools a provisioned server lists."""
    exported = tds.export_tools(SRC)

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

    exported = tds.export_tools(SRC)

    assert [item["name"] for item in exported["tools"]] == MCP_TOOL_NAMES


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


def test_a_revision_and_a_changed_working_tree_are_told_apart(tmp_path: Path) -> None:
    """The base comes from a git worktree of the ref; the head is the working tree.
    In the working tree of a test copy a tool is added, one is changed and one removed."""
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

    base = tds.export_revision(repo, "HEAD")
    head = tds.export_tools(repo / "src")

    assert base["tools"] == provisioned_listing(tmp_path / "workspace")
    diff = tds.diff_definitions(base["tools"], head["tools"])
    assert list(diff.added) == ["tdqs_added_tool"]
    assert list(diff.removed) == [last]
    assert list(diff.changed) == [first]
    assert base["setHash"] != head["setHash"]
    worktrees = [line for line in git(repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]
    assert len(worktrees) == 1
    assert git(repo, "status", "--porcelain").split() == ["M", "src/agentic_hil/contracts.py"]
