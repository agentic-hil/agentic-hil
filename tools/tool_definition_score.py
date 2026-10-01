"""Keep the server's Tool Definition Quality Score from falling.

A change passes when the overall score of the tool definitions after it is at
least the overall score before it. The number decides, not the letter tier: a
drop from 3.7 to 3.6 blocks although both are an A.

The score is the TDQS method, version 1.3, as published at the pinned upstream
commit recorded in tools/tdqs/version.json: context signals and invocation
cost, the hard gates, one rubric call per tool, one coherence call per set, and
the published rollups. Every rounding goes through the integer round1 on exact
rationals, so a sum that lands exactly on a tie is never pushed below it by a
binary float.

Both sides are scored complete. Adding a tool changes the sibling list of every
other tool and the coherence of the set, so scoring only the changed tool would
compare two different questions. When the two sets are identical nothing is
scored and the check passes without a model.

The upstream specification carries no license, so its prompt texts are not in
this repository. They are fetched from the pinned commit, checked against the
sha256 digests in the version record, and cached outside the tree.

Run it from the repository root:

    python tools/tool_definition_score.py --base origin/master

The head is the working tree. Exit codes: 0 pass, 1 block, 2 invalid. The JSON
report and its Markdown summary are written to .tool-definition-score/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, NamedTuple

TOOLS_DIRECTORY = Path(__file__).resolve().parent
TDQS_DIRECTORY = TOOLS_DIRECTORY / "tdqs"
VERSION_RECORD = TDQS_DIRECTORY / "version.json"
CALIBRATION_RECORD = TDQS_DIRECTORY / "calibration.json"
REPORT_JSON = "tool-definition-score.json"
REPORT_MARKDOWN = "tool-definition-score.md"
UPSTREAM_README = "https://raw.githubusercontent.com/glama-ai/tool-definition-quality-score/{commit}/README.md"

# Computing the score: the six dimensions in their published order, with their
# integer weights in hundredths.
DIMENSIONS = (
    "purpose_clarity",
    "usage_guidelines",
    "behavioral_transparency",
    "parameter_semantics",
    "conciseness_structure",
    "contextual_completeness",
)
DIMENSION_WEIGHTS = {
    "purpose_clarity": 25,
    "usage_guidelines": 20,
    "behavioral_transparency": 20,
    "parameter_semantics": 15,
    "conciseness_structure": 10,
    "contextual_completeness": 10,
}
DESCRIPTION_QUALITY_WEIGHTS = {"mean": 60, "minimum": 40}
OVERALL_WEIGHTS = {"description_quality": 70, "coherence": 30}
TIER_THRESHOLDS = {"A": Fraction(7, 2), "B": Fraction(3), "C": Fraction(2), "D": Fraction(1)}
# Appendix B names the coherence dimensions in snake case; the published server
# result names them in camel case.
COHERENCE_DIMENSIONS = ("disambiguation", "naming_consistency", "tool_count_appropriateness", "completeness")
COHERENCE_FIELDS = {
    "disambiguation": "disambiguation",
    "naming_consistency": "namingConsistency",
    "tool_count_appropriateness": "toolCountAppropriateness",
    "completeness": "completeness",
}
PROMPT_KEYS = ("coherence_system", "coherence_user", "tool_system", "tool_user")

# Two retries: a third invalid answer in a row is a backend that is not
# answering the contract, and more attempts would only hide it.
RETRIES = 2
# Fixed in advance and versioned: one pair, and on a drop exactly two more,
# decided by the median overall of each side.
CONFIRMATION = {"version": 1, "initial_pairs": 1, "confirmation_pairs_on_drop": 2, "statistic": "median_per_side"}
EXIT_CODES = {"pass": 0, "block": 1, "invalid": 2}

FLAG_NO_DESCRIPTION = "No Description"
FLAG_TAUTOLOGY = "Tautological Description"
FLAG_CONTRADICTION = "Annotation Contradiction"
FLAG_SHADOWING = "Shadowing Risk"

# Stage 1: the traversal of the required subtree stops below this level.
MAXIMUM_SCHEMA_LEVEL = 10
ANNOTATION_HINTS = {"readOnly": "readOnlyHint", "destructive": "destructiveHint", "idempotent": "idempotentHint", "openWorld": "openWorldHint"}
DEFINITION_FIELDS = ("name", "title", "description", "inputSchema", "outputSchema", "annotations")

MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_TIMEOUT_S = 120.0
GIT_TIMEOUT_S = 120.0
CLI_TIMEOUT_S = 180.0
VERSION_TIMEOUT_S = 60.0
FETCH_TIMEOUT_S = 60.0
DEFAULT_CONCURRENCY = 6


class InvalidComparison(Exception):
    """The comparison cannot be decided: it never passes, it is reported as invalid."""


class IncompleteResult(InvalidComparison):
    """A result lacks a tool, a side or a part the decision needs."""


class StaleReport(InvalidComparison):
    """A saved report scored other definitions or other versions than the current ones."""


class PromptMismatch(InvalidComparison):
    """The prompts are not the ones the version record pins."""


class InvalidAnswer(ValueError):
    """A model answer that does not follow the output contract."""


class BackendError(RuntimeError):
    """The model call itself failed."""


class DefinitionDiff(NamedTuple):
    added: list[str]
    removed: list[str]
    changed: list[str]
    unchanged: list[str]


# --- exact arithmetic (Computing the score, Tiers, Server-level scores, Overall) ---


def _exact(value: object) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, Fraction)):
        raise TypeError(f"expected an int or a Fraction, got {type(value).__name__}")
    return Fraction(value)


def round1(p: int | Fraction, q: int | Fraction) -> Fraction:
    """p / q rounded half up to one decimal: floor((20p + q) / (2q)) / 10, on exact rationals."""
    p, q = _exact(p), _exact(q)
    if q <= 0:
        raise ValueError("round1 needs a positive denominator")
    return Fraction(math.floor((20 * p + q) / (2 * q)), 10)


def tier(score: Fraction) -> str:
    for letter, threshold in TIER_THRESHOLDS.items():
        if score >= threshold:
            return letter
    return "F"


def compute_tdqs(scores: dict[str, int]) -> Fraction:
    return round1(sum(DIMENSION_WEIGHTS[dimension] * _exact(scores[dimension]) for dimension in DIMENSIONS), 100)


def description_quality(values: Sequence[Fraction]) -> Fraction:
    """60 % of the exact mean plus 40 % of the minimum, rounded once."""
    if not values:
        raise IncompleteResult("There is no tool to compute description quality from.")
    total = sum((_exact(value) for value in values), Fraction(0))
    count = len(values)
    return round1(DESCRIPTION_QUALITY_WEIGHTS["mean"] // 10 * total + DESCRIPTION_QUALITY_WEIGHTS["minimum"] // 10 * count * min(values), 10 * count)


def coherence_score(values: dict[str, int]) -> Fraction:
    return round1(sum(_exact(values[dimension]) for dimension in COHERENCE_DIMENSIONS), len(COHERENCE_DIMENSIONS))


def overall_score(quality: Fraction, coherence: Fraction) -> Fraction:
    return round1(OVERALL_WEIGHTS["description_quality"] // 10 * _exact(quality) + OVERALL_WEIGHTS["coherence"] // 10 * _exact(coherence), 10)


def _unrounded_quality(values: Sequence[Fraction]) -> Fraction:
    return Fraction(DESCRIPTION_QUALITY_WEIGHTS["mean"], 100) * sum(values, Fraction(0)) / len(values) + Fraction(DESCRIPTION_QUALITY_WEIGHTS["minimum"], 100) * min(values)


def rollups(results: dict[str, dict], names: Sequence[str], coherence: dict) -> dict:
    """The server-level scores. Every tool must be scored: the registry's 80 % allowance does not apply here."""
    if not names:
        raise IncompleteResult("The tool set is empty, so there is nothing to score.")
    missing = [name for name in names if name not in results]
    if missing:
        raise IncompleteResult(f"These tools have no score: {', '.join(missing)}.")
    values = [_exact(results[name]["tdqs"]) for name in names]
    minimum_tool = min(names, key=lambda name: (_exact(results[name]["tdqs"]), name))
    quality = description_quality(values)
    coherence_value = _exact(coherence["coherenceScore"])
    overall = overall_score(quality, coherence_value)
    return {
        "toolCount": len(names),
        "scoredToolCount": len(names),
        "meanTdqs": round1(sum(values, Fraction(0)), len(values)),
        "minTdqs": min(values),
        "minTool": minimum_tool,
        "descriptionQualityScore": quality,
        "descriptionQualityTier": tier(quality),
        "coherenceScore": coherence_value,
        "coherenceTier": tier(coherence_value),
        "overallScore": overall,
        "overallTier": tier(overall),
    }


# --- definitions, hashes and change detection ------------------------------------------


def canonical_definition(definition: dict) -> bytes:
    """The six definition fields, an absent one as null, as sorted compact UTF-8 JSON."""
    return json.dumps({field: definition.get(field) for field in DEFINITION_FIELDS}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def definition_hash(definition: dict) -> str:
    return hashlib.sha256(canonical_definition(definition)).hexdigest()


def set_hash(tools: Iterable[dict]) -> str:
    """The hash of the set: independent of listing order, moved by any field of any tool."""
    return hashlib.sha256("\n".join(sorted(definition_hash(item) for item in tools)).encode("utf-8")).hexdigest()


def diff_definitions(base_tools: Iterable[dict], head_tools: Iterable[dict]) -> DefinitionDiff:
    base = {item["name"]: definition_hash(item) for item in base_tools}
    head = {item["name"]: definition_hash(item) for item in head_tools}
    return DefinitionDiff(
        added=sorted(set(head) - set(base)),
        removed=sorted(set(base) - set(head)),
        changed=sorted(name for name in set(base) & set(head) if base[name] != head[name]),
        unchanged=sorted(name for name in set(base) & set(head) if base[name] == head[name]),
    )


def _size(tools: list[dict]) -> dict:
    return {
        "descriptionCharacters": sum(len(item["description"]) for item in tools if isinstance(item.get("description"), str)),
        "toolsListBytes": len(json.dumps({"tools": tools}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        "definitionBytes": {item["name"]: len(canonical_definition(item)) for item in tools},
    }


def export_from_tools(tools: list[dict], server_name: str, server_version: str) -> dict:
    """An export: the definitions as tools/list returned them, their hashes and what they cost in size."""
    duplicates = [name for name, count in Counter(item.get("name") for item in tools).items() if count > 1]
    if duplicates:
        raise InvalidComparison(f"tools/list names {', '.join(map(str, duplicates))} more than once.")
    return {
        "serverName": server_name,
        "serverVersion": server_version,
        "tools": tools,
        "hashes": {item["name"]: definition_hash(item) for item in tools},
        "setHash": set_hash(tools),
        "size": _size(tools),
    }


def size_report(base_export: dict, head_export: dict) -> dict:
    """Information for the reviewer, never part of the decision."""
    base = base_export.get("size") or _size(base_export["tools"])
    head = head_export.get("size") or _size(head_export["tools"])
    names = sorted(set(base["definitionBytes"]) | set(head["definitionBytes"]))
    return {
        "base": base,
        "head": head,
        "delta": {
            "descriptionCharacters": head["descriptionCharacters"] - base["descriptionCharacters"],
            "toolsListBytes": head["toolsListBytes"] - base["toolsListBytes"],
            "definitionBytes": {name: head["definitionBytes"].get(name, 0) - base["definitionBytes"].get(name, 0) for name in names},
        },
    }


# --- Stage 1: context signals and invocation cost ----------------------------------------


def _type_includes(node: dict, name: str) -> bool:
    declared = node.get("type")
    return declared == name or (isinstance(declared, list) and name in declared)


def _is_object(node: dict) -> bool:
    return _type_includes(node, "object") or "properties" in node or "required" in node


def _pointer(root: dict, reference: str) -> object:
    if not reference.startswith("#"):
        return None
    node: object = root
    for part in reference[1:].split("/")[1:] if reference != "#" else []:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def _measure(node: object, root: dict, level: int, ancestors: frozenset[str], inherited: dict | None = None) -> tuple[int, int, int]:
    """(required fields, depth, union choices) of the required subtree under `node`.

    A reference is followed; a pointer already on the path from the root is a
    leaf, so a recursive definition stops at its repeat while one definition
    required twice is counted twice."""
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        reference = node["$ref"]
        if reference in ancestors:
            return 0, 0, 0
        ancestors = ancestors | {reference}
        node = _pointer(root, reference)
    if not isinstance(node, dict):
        return 0, 0, 0
    if not _is_object(node) and isinstance(node.get("items"), dict):
        return _measure(node["items"], root, level, ancestors)
    count, depth, unions = 0, 1 if _is_object(node) else 0, 0
    properties = dict(inherited or {})
    if isinstance(node.get("properties"), dict):
        properties.update(node["properties"])
    required = node.get("required") if isinstance(node.get("required"), list) else []
    for name in required:
        if not isinstance(name, str):
            continue
        count += 1
        if level >= MAXIMUM_SCHEMA_LEVEL:
            continue
        child_count, child_depth, child_unions = _measure(properties.get(name, {}), root, level + 1, ancestors)
        count += child_count
        unions += child_unions
        depth = max(depth, 1 + child_depth)
    branches = node.get("allOf")
    if isinstance(branches, list):
        for branch in branches:
            branch_count, branch_depth, branch_unions = _measure(branch, root, level, ancestors, properties)
            count += branch_count
            depth = max(depth, branch_depth)
            unions += branch_unions
    for keyword in ("oneOf", "anyOf"):
        branches = node.get(keyword)
        if not isinstance(branches, list) or not branches:
            continue
        measured = [_measure(branch, root, level, ancestors, properties) for branch in branches]
        count += max(item[0] for item in measured)
        depth = max(depth, max(item[1] for item in measured))
        unions += sum(item[2] for item in measured)
        # A union of one type with null is the nullable idiom, not a choice.
        choices = [branch for branch in branches if not (isinstance(branch, dict) and branch.get("type") == "null")]
        unions += len(choices) - 1 if len(choices) > 1 else 0
    if isinstance(node.get("not"), dict):
        depth = max(depth, _measure(node["not"], root, level, ancestors, properties)[1])
    return count, depth, unions


def _invocation(schema: object) -> tuple[int, int, int, int]:
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict) or not schema["properties"]:
        return 0, 0, 0, 0
    count, depth, unions = _measure(schema, schema, 1, frozenset())
    return count, depth, unions, count + 2 * max(0, depth - 1) + 2 * unions


def context_signals(definition: dict) -> dict:
    schema = definition.get("inputSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    properties = properties if isinstance(properties, dict) else {}
    required = schema.get("required") if isinstance(schema, dict) else None
    required = required if isinstance(required, list) else []
    described = sum(1 for value in properties.values() if isinstance(value, dict) and isinstance(value.get("description"), str) and value["description"].strip())
    total = len(properties)
    coverage = 100 if total == 0 else (200 * described + total) // (2 * total)
    count, depth, unions, cost = _invocation(schema)
    annotations = definition.get("annotations")
    hints = annotations if isinstance(annotations, dict) else {}
    output_schema = definition.get("outputSchema")
    title = definition.get("title")
    name = definition.get("name", "")
    canonical = canonical_definition(definition)
    return {
        "paramCount": total,
        "requiredParamCount": len(required),
        "paramsWithDescriptions": described,
        "paramsWithEnums": sum(1 for value in properties.values() if isinstance(value, dict) and "enum" in value),
        "schemaDescriptionCoverage": coverage,
        "hasNestedObjects": any(isinstance(value, dict) and _type_includes(value, "object") for value in properties.values()),
        "requiredFieldCount": count,
        "schemaDepth": depth,
        "unionChoiceCount": unions,
        "invocationCost": cost,
        "hasOutputSchema": isinstance(output_schema, dict) and bool(output_schema),
        "hasAnnotations": isinstance(annotations, dict) and bool(annotations),
        "annotationValues": {key: hints.get(hint) if isinstance(hints.get(hint), bool) else None for key, hint in ANNOTATION_HINTS.items()},
        "titleIsMeaningful": isinstance(title, str) and title != name and len(title) > len(name),
        "definitionBytes": len(canonical),
        "inputHash": hashlib.sha256(canonical).hexdigest()[:16],
    }


# --- the prompts: fetched at the pinned commit, extracted and verified ----------------------


def upstream_readme_url(commit: str) -> str:
    return UPSTREAM_README.format(commit=commit)


def _sections(readme: str) -> dict[str, list[str]]:
    """Second-level sections; a heading inside a fenced block is part of the block."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    in_fence = False
    for line in readme.split("\n"):
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and line.startswith("## "):
            current = sections.setdefault(line[3:].strip(), [])
            continue
        if current is not None:
            current.append(line)
    return sections


def _text_blocks(lines: list[str]) -> list[str]:
    blocks: list[str] = []
    buffer: list[str] | None = None
    inside = False
    for line in lines:
        if not inside and line.startswith("```"):
            inside = True
            buffer = [] if line.strip() == "```text" else None
        elif inside and line.strip() == "```":
            inside = False
            if buffer is not None:
                blocks.append("\n".join(buffer))
        elif inside and buffer is not None:
            buffer.append(line)
    return blocks


def extract_prompts(readme: str) -> dict[str, str]:
    """The system prompt and the user template of Appendix A and Appendix B."""
    sections = _sections(readme)
    prompts: dict[str, str] = {}
    for appendix, prefix in (("Appendix A", "tool"), ("Appendix B", "coherence")):
        heading = next((title for title in sections if title.startswith(appendix)), None)
        if heading is None:
            raise PromptMismatch(f"The specification has no {appendix}, so the {prefix}_system and {prefix}_user prompts cannot be read.")
        blocks = _text_blocks(sections[heading])
        if len(blocks) < 2:
            raise PromptMismatch(f"{appendix} holds {len(blocks)} text blocks, not the {prefix}_system prompt and the {prefix}_user template.")
        prompts[f"{prefix}_system"], prompts[f"{prefix}_user"] = blocks[0], blocks[1]
    return prompts


def _prompt_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_prompts(prompts: dict[str, str], hashes: dict[str, str]) -> None:
    for key in PROMPT_KEYS:
        if key not in hashes:
            raise PromptMismatch(f"The version record has no sha256 for the {key} prompt.")
        if key not in prompts:
            raise PromptMismatch(f"The specification has no {key} prompt.")
        if _prompt_digest(prompts[key]) != hashes[key]:
            raise PromptMismatch(f"The fetched {key} prompt does not match its recorded sha256.")


def default_cache_dir(environ: dict[str, str] | None = None) -> Path:
    """Outside the repository: the user's cache directory."""
    environ = dict(os.environ) if environ is None else environ
    base = environ.get("XDG_CACHE_HOME") or environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "agentic-hil-tdqs"


def fetch_url(url: str) -> str:
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as response:  # noqa: S310 - a fixed https URL
        return response.read().decode("utf-8")


def load_prompts(record: dict, cache_dir: Path | str, fetch: Callable[[str], str]) -> dict[str, str]:
    """The four prompts, from the cache when it still verifies, else fetched once.

    A fetched README is kept only when its prompts verify, so a changed
    upstream text never lands in the cache."""
    commit = record["spec_commit"]
    hashes = record["prompt_sha256"]
    cache = Path(cache_dir) / f"{commit}.md"
    if cache.is_file():
        try:
            prompts = extract_prompts(cache.read_bytes().decode("utf-8"))
            verify_prompts(prompts, hashes)
            return prompts
        except (PromptMismatch, UnicodeDecodeError):
            pass
    text = fetch(upstream_readme_url(commit))
    prompts = extract_prompts(text)
    verify_prompts(prompts, hashes)
    cache.parent.mkdir(parents=True, exist_ok=True)
    partial = cache.with_name(f"{cache.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    partial.write_bytes(text.encode("utf-8"))
    os.replace(partial, cache)
    return prompts


# --- the prompts as the model receives them (Appendix A, Appendix B) -----------------------

TOOL_PROMPT_WITHHELD = {"invocationCost", "requiredFieldCount", "schemaDepth", "unionChoiceCount", "definitionBytes", "hasOutputSchema"}
ONE_PER_LINE = re.compile(r',\s*one per line(?:,[^|]*)?\s*(?:\|\s*"(.*)")?\s*', re.DOTALL)
FALLBACK = re.compile(r'(.*?)\s*\|\s*"(.*)"', re.DOTALL)


class _Placeholder(NamedTuple):
    start: int
    end: int
    token: str
    fallback: str | None
    line_format: str | None


def _placeholders(template: str) -> list[_Placeholder]:
    """Every top-level {placeholder}; braces inside a quoted part of one are its text."""
    found = []
    position = 0
    while (start := template.find("{", position)) >= 0:
        depth, quoted, index = 0, False, start
        while index < len(template):
            character = template[index]
            if quoted:
                quoted = character != '"'
            elif character == '"':
                quoted = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        else:
            raise PromptMismatch(f"The template has an unclosed placeholder at {template[start:start + 40]!r}.")
        content = template[start + 1:index].strip()
        if content.startswith('"'):
            closing = content.find('"', 1)
            match = ONE_PER_LINE.fullmatch(content[closing + 1:]) if closing > 0 else None
            if match is None:
                raise PromptMismatch(f"The template has a placeholder this gate does not know: {content}")
            found.append(_Placeholder(start, index + 1, content[1:closing], match.group(1), content[1:closing]))
        else:
            match = FALLBACK.fullmatch(content)
            token, fallback = (match.group(1), match.group(2)) if match else (content, None)
            found.append(_Placeholder(start, index + 1, token.strip(), fallback, None))
        position = index + 1
    return found


def _missing(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, list) and not value)


def _substitute(template: str, resolve: Callable[[_Placeholder], str]) -> str:
    parts, position = [], 0
    for placeholder in _placeholders(template):
        parts.append(template[position:placeholder.start])
        parts.append(resolve(placeholder))
        position = placeholder.end
    parts.append(template[position:])
    return "".join(parts)


def _filled(value: object, placeholder: _Placeholder) -> str:
    if _missing(value):
        return placeholder.fallback if placeholder.fallback is not None else ""
    return value if isinstance(value, str) else str(value)


def _pretty(value: object) -> str | None:
    return None if value is None else json.dumps(value, indent=2, ensure_ascii=False)


def render_tool_prompt(template: str, definition: dict, siblings: Sequence[str]) -> str:
    """The Appendix A user message for one tool: the full definition and the sibling names."""
    signals = context_signals(definition)
    title = definition.get("title")
    values: dict[str, object] = {
        "name": definition.get("name"),
        "title": title if isinstance(title, str) else None,
        "description": definition.get("description") if isinstance(definition.get("description"), str) else None,
        "inputSchema JSON": _pretty(definition.get("inputSchema")),
        "outputSchema JSON": _pretty(definition.get("outputSchema")),
        "annotations JSON": _pretty(definition.get("annotations")),
        "paramCount": signals["paramCount"],
        "requiredParamCount": signals["requiredParamCount"],
        "schemaDescriptionCoverage": signals["schemaDescriptionCoverage"],
        "paramsWithEnums": signals["paramsWithEnums"],
        "hasNestedObjects": "true" if signals["hasNestedObjects"] else "false",
        "sibling tool names, one per line": "\n".join(siblings) or None,
    }

    def resolve(placeholder: _Placeholder) -> str:
        if placeholder.line_format is not None or placeholder.token not in values:
            if placeholder.token in TOOL_PROMPT_WITHHELD:
                raise PromptMismatch(f"The tool template asks for {placeholder.token}, which the tool prompt withholds.")
            raise PromptMismatch(f"The tool template has a placeholder this gate does not know: {placeholder.token}")
        return _filled(values[placeholder.token], placeholder)

    return _substitute(template, resolve)


def render_coherence_prompt(template: str, server_name: str, tools: Sequence[dict], candidates: Sequence[dict]) -> str:
    """The Appendix B user message: every tool with its cost, and the shadow candidates.

    The template line that names a tool is repeated once per tool, and the
    "- ..." line after it, which stands for the rest of the list, is dropped."""
    lines = template.split("\n")
    rendered: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        placeholders = _placeholders(line)
        if any(placeholder.token == "name" and placeholder.line_format is None for placeholder in placeholders):
            for item in tools:
                signals = context_signals(item)
                values: dict[str, object] = {
                    "name": item.get("name"),
                    "invocationCost": signals["invocationCost"],
                    "requiredFieldCount": signals["requiredFieldCount"],
                    "schemaDepth": signals["schemaDepth"],
                    "unionChoiceCount": signals["unionChoiceCount"],
                    "description": item.get("description") if isinstance(item.get("description"), str) else None,
                }
                rendered.append(_substitute(line, lambda placeholder, values=values: _coherence_value(placeholder, values)))
            if index + 1 < len(lines) and lines[index + 1].strip() == "- ...":
                index += 1
        else:
            values = {"serverName": server_name, "toolCount": len(tools)}
            rendered.append(_substitute(line, lambda placeholder, values=values: _coherence_value(placeholder, values, candidates)))
        index += 1
    return "\n".join(rendered)


def _coherence_value(placeholder: _Placeholder, values: dict, candidates: Sequence[dict] | None = None) -> str:
    if placeholder.line_format is not None and candidates is not None:
        fields = {"dearer": "tool", "n": "invocationCost", "cheaper": "cheaperSibling", "m": "cheaperSiblingInvocationCost"}

        def candidate_line(candidate: dict) -> str:
            def field(inner: _Placeholder) -> str:
                if inner.token not in fields:
                    raise PromptMismatch(f"The coherence template has a placeholder this gate does not know: {inner.token}")
                return str(candidate[fields[inner.token]])

            return _substitute(placeholder.line_format or "", field)

        return _filled([candidate_line(candidate) for candidate in candidates] and "\n".join(candidate_line(candidate) for candidate in candidates), placeholder)
    if placeholder.line_format is not None or placeholder.token not in values:
        raise PromptMismatch(f"The coherence template has a placeholder this gate does not know: {placeholder.token}")
    return _filled(values[placeholder.token], placeholder)


# --- the output contract (LLM output contract) ----------------------------------------------

FENCE = re.compile(r"\A```(?:json)?[ \t]*\n(.*)\n```\s*\Z", re.DOTALL)


def _decode(text: object) -> dict:
    if not isinstance(text, str):
        raise InvalidAnswer("The answer is not text.")
    stripped = text.strip()
    fenced = FENCE.match(stripped)
    if fenced:
        stripped = fenced.group(1)
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError as error:
        raise InvalidAnswer(f"The answer is not JSON: {error}") from None
    if not isinstance(decoded, dict):
        raise InvalidAnswer("The answer is not a JSON object.")
    return decoded


def _scored(answer: dict, dimensions: Sequence[str]) -> dict[str, dict]:
    scores = answer.get("scores")
    if not isinstance(scores, dict):
        raise InvalidAnswer("The answer has no scores object.")
    parsed = {}
    for dimension in dimensions:
        entry = scores.get(dimension)
        if not isinstance(entry, dict):
            raise InvalidAnswer(f"The answer has no {dimension} object.")
        score = entry.get("score")
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise InvalidAnswer(f"The {dimension} score is not an integer from 1 to 5: {score!r}")
        if not isinstance(entry.get("justification"), str):
            raise InvalidAnswer(f"The {dimension} justification is not a string.")
        parsed[dimension] = {"score": score, "justification": entry["justification"]}
    return parsed


def parse_tool_answer(text: str) -> dict:
    answer = _decode(text)
    scores = _scored(answer, DIMENSIONS)
    if not isinstance(answer.get("annotation_contradiction"), bool):
        raise InvalidAnswer("annotation_contradiction is not a boolean.")
    if not isinstance(answer.get("summary"), str):
        raise InvalidAnswer("summary is not a string.")
    return {"scores": scores, "annotation_contradiction": answer["annotation_contradiction"], "summary": answer["summary"]}


def parse_coherence_answer(text: str) -> dict:
    answer = _decode(text)
    scores = _scored(answer, COHERENCE_DIMENSIONS)
    risks = answer.get("shadowing_risks")
    if not isinstance(risks, list):
        raise InvalidAnswer("shadowing_risks is not a list.")
    for entry in risks:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(field), str) for field in ("tool", "cheaper_sibling", "justification")):
            raise InvalidAnswer(f"A shadowing risk is not an object with tool, cheaper_sibling and justification strings: {entry!r}")
    if not isinstance(answer.get("summary"), str):
        raise InvalidAnswer("summary is not a string.")
    return {"scores": scores, "shadowing_risks": risks, "summary": answer["summary"]}


def _ask(call: Callable[[], str], parse: Callable[[str], dict], subject: str) -> dict:
    """One call plus RETRIES more while the answer is invalid or the backend fails."""
    last: Exception | None = None
    for _ in range(1 + RETRIES):
        try:
            return parse(call())
        except (InvalidAnswer, BackendError) as error:
            last = error
    raise InvalidComparison(f"The answer for {subject} was still not valid after {RETRIES} retries: {last}")


# --- Stage 2 to Stage 4: one tool -----------------------------------------------------------

NO_DESCRIPTION_JUSTIFICATION = "The tool has no description, so there is nothing to evaluate; the hard gate scores every dimension 1 without a model call."


def _tautological(definition: dict) -> bool:
    text = definition["description"].strip().lower()
    name, title = definition.get("name"), definition.get("title")
    return (isinstance(name, str) and text == name.strip().lower()) or (isinstance(title, str) and text == title.strip().lower())


def evaluate_tool(definition: dict, siblings: Sequence[str], scorer: Any) -> dict:
    name = definition["name"]
    description = definition.get("description")
    flags: list[str] = []
    if not isinstance(description, str) or not description.strip():
        scores = dict.fromkeys(DIMENSIONS, 1)
        justifications = {dimension: {"score": 1, "justification": NO_DESCRIPTION_JUSTIFICATION} for dimension in DIMENSIONS}
        flags.append(FLAG_NO_DESCRIPTION)
        summary = "No description."
        contradiction = False
    else:
        sibling_names = list(siblings)
        answer = _ask(lambda: scorer.tool_answer(definition, sibling_names), parse_tool_answer, f"tool {name}")
        justifications = {dimension: dict(answer["scores"][dimension]) for dimension in DIMENSIONS}
        if _tautological(definition):
            flags.append(FLAG_TAUTOLOGY)
            capped = min(justifications["purpose_clarity"]["score"], 2)
            justifications["purpose_clarity"]["score"] = capped
        contradiction = answer["annotation_contradiction"]
        if contradiction:
            flags.append(FLAG_CONTRADICTION)
        scores = {dimension: justifications[dimension]["score"] for dimension in DIMENSIONS}
        summary = answer["summary"]
    tdqs = compute_tdqs(scores)
    return {
        "name": name,
        "definitionHash": definition_hash(definition),
        "contextSignals": context_signals(definition),
        "scores": scores,
        "justifications": justifications,
        "tdqs": tdqs,
        "tier": tier(tdqs),
        "flags": flags,
        "smells": [dimension for dimension in DIMENSIONS if scores[dimension] < 3],
        "summary": summary,
        "serverFlags": [],
        "annotationContradiction": contradiction,
    }


# --- coherence and shadowed tools ------------------------------------------------------------


def is_shadow_candidate(cheap: int, expensive: int) -> bool:
    return expensive >= 2 * cheap and expensive - cheap >= 4


def shadow_candidates(tools: Sequence[dict]) -> list[dict]:
    """At most one candidate per tool: the dearest sibling it may be shadowed by."""
    costs = {item["name"]: context_signals(item)["invocationCost"] for item in tools}
    candidates = []
    for name in sorted(costs):
        qualifying = sorted(((cost, other) for other, cost in costs.items() if other != name and is_shadow_candidate(cost, costs[name])), key=lambda pair: (-pair[0], pair[1]))
        if qualifying:
            cost, other = qualifying[0]
            candidates.append({"tool": name, "invocationCost": costs[name], "cheaperSibling": other, "cheaperSiblingInvocationCost": cost})
    return candidates


def confirmed_shadowing_risks(entries: Sequence[dict], candidates: Sequence[dict]) -> list[dict]:
    """The four referential checks: both names exist, the pair was supplied, the tool is the dearer side, no tool twice."""
    names = {candidate["tool"] for candidate in candidates} | {candidate["cheaperSibling"] for candidate in candidates}
    supplied = {(candidate["tool"], candidate["cheaperSibling"]): candidate for candidate in candidates}
    confirmed, seen = [], set()
    for entry in entries:
        pair = (entry["tool"], entry["cheaper_sibling"])
        if pair[0] not in names or pair[1] not in names or pair not in supplied or pair[0] in seen:
            continue
        candidate = supplied[pair]
        if candidate["invocationCost"] <= candidate["cheaperSiblingInvocationCost"]:
            continue
        seen.add(pair[0])
        confirmed.append({**candidate, "justification": entry["justification"]})
    return confirmed


def evaluate_coherence(server_name: str, tools: Sequence[dict], scorer: Any) -> dict:
    candidates = shadow_candidates(tools)
    answer = _ask(lambda: scorer.coherence_answer(server_name, list(tools), candidates), parse_coherence_answer, "the set's coherence")
    values = {dimension: answer["scores"][dimension]["score"] for dimension in COHERENCE_DIMENSIONS}
    score = coherence_score(values)
    return {
        **{COHERENCE_FIELDS[dimension]: values[dimension] for dimension in COHERENCE_DIMENSIONS},
        "coherenceScore": score,
        "coherenceTier": tier(score),
        "shadowingRisks": confirmed_shadowing_risks(answer["shadowing_risks"], candidates),
        "coherenceSummary": answer["summary"],
        "coherenceJustifications": {dimension: dict(answer["scores"][dimension]) for dimension in COHERENCE_DIMENSIONS},
    }


def score_side(exported: dict, scorer: Any, version_digest: str) -> dict:
    """Every tool of one complete set, each with the full sibling list, plus the set's coherence."""
    tools = exported["tools"]
    names = [item["name"] for item in tools]
    workers = max(1, min(len(tools) + 1, int(getattr(scorer, "concurrency", DEFAULT_CONCURRENCY) or DEFAULT_CONCURRENCY)))
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tdqs")
    try:
        tool_futures = [pool.submit(evaluate_tool, item, [other for other in names if other != item["name"]], scorer) for item in tools]
        coherence_future = pool.submit(evaluate_coherence, exported["serverName"], tools, scorer)
        everything = [*tool_futures, coherence_future]
        done, _ = wait(everything, return_when=FIRST_EXCEPTION)
        for future in everything:
            if future in done and future.exception() is not None:
                raise future.exception()  # type: ignore[misc]
        results = {name: future.result() for name, future in zip(names, tool_futures, strict=True)}
        coherence = coherence_future.result()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    for risk in coherence["shadowingRisks"]:
        results[risk["tool"]]["serverFlags"] = [FLAG_SHADOWING]
    return {
        "serverName": exported["serverName"],
        "setHash": exported["setHash"],
        "versionDigest": version_digest,
        "tools": results,
        "coherence": coherence,
        "rollups": rollups(results, names, coherence),
    }


# --- the confirmation procedure --------------------------------------------------------------


def _number(value: object) -> Fraction:
    """A score as an exact rational; a JSON float is read back from its shortest repr."""
    if isinstance(value, bool):
        raise InvalidComparison(f"A score is a boolean: {value!r}")
    if isinstance(value, (int, Fraction)):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(repr(value))
    if isinstance(value, str):
        return Fraction(value)
    raise InvalidComparison(f"A score is not a number: {value!r}")


def _sides(pair: object) -> tuple[dict, dict]:
    if isinstance(pair, dict):
        return pair["base"], pair["head"]
    base, head = pair  # type: ignore[misc]
    return base, head


def _overall(side: dict) -> Fraction:
    return _number(side["rollups"]["overallScore"])


def _median(values: Sequence[Fraction]) -> Fraction:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def medians(pairs: Sequence[object]) -> dict[str, Fraction]:
    sides = [_sides(pair) for pair in pairs]
    return {"base": _median([_overall(base) for base, _ in sides]), "head": _median([_overall(head) for _, head in sides])}


def decide(pairs: Sequence[object]) -> str:
    """One pair passes when the head holds; a drop needs exactly two more pairs and the per-side medians decide."""
    if not pairs:
        raise IncompleteResult("No pair was scored.")
    base, head = _sides(pairs[0])
    if _overall(head) >= _overall(base):
        if len(pairs) != CONFIRMATION["initial_pairs"]:
            raise InvalidComparison(f"The first pair held, so there must be exactly one pair, not {len(pairs)}.")
        return "pass"
    expected = CONFIRMATION["initial_pairs"] + CONFIRMATION["confirmation_pairs_on_drop"]
    if len(pairs) != expected:
        raise InvalidComparison(f"The first pair dropped, so there must be exactly {expected} pairs, not {len(pairs)}.")
    middle = medians(pairs)
    return "block" if middle["head"] < middle["base"] else "pass"


def confirm(run_pair: Callable[[], tuple[dict, dict]]) -> dict:
    pairs = [run_pair()]
    base, head = pairs[0]
    if _overall(head) < _overall(base):
        pairs.extend(run_pair() for _ in range(CONFIRMATION["confirmation_pairs_on_drop"]))
    return {"decision": decide(pairs), "pairs": [{"base": base, "head": head} for base, head in pairs], "median": medians(pairs)}


# --- the comparison and its report -----------------------------------------------------------


def version_digest(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _decimal(value: Fraction, places: int = 1) -> str:
    return format(Decimal(value.numerator) / Decimal(value.denominator), f".{places}f")


def _causes(base: dict, head: dict) -> dict:
    """What fell on the first pair, and how much each fall alone costs the unrounded overall."""
    base_values = {name: _number(result["tdqs"]) for name, result in base["tools"].items()}
    head_values = {name: _number(result["tdqs"]) for name, result in head["tools"].items()}
    names = list(base_values)
    weight = Fraction(OVERALL_WEIGHTS["description_quality"], 100)
    before = _unrounded_quality([base_values[name] for name in names])
    tools = []
    for name in names:
        if name not in head_values or head_values[name] >= base_values[name]:
            continue
        alone = _unrounded_quality([head_values[other] if other == name else base_values[other] for other in names])
        dimensions = [
            {
                "dimension": dimension,
                "base": base["tools"][name]["scores"][dimension],
                "head": head["tools"][name]["scores"][dimension],
                "justification": head["tools"][name]["justifications"][dimension]["justification"],
            }
            for dimension in DIMENSIONS
            if head["tools"][name]["scores"][dimension] < base["tools"][name]["scores"][dimension]
        ]
        tools.append({"tool": name, "base": base_values[name], "head": head_values[name], "effect": weight * (before - alone), "dimensions": dimensions})
    tools.sort(key=lambda item: (-item["effect"], item["tool"]))
    coherence = [
        {
            "dimension": dimension,
            "base": base["coherence"][COHERENCE_FIELDS[dimension]],
            "head": head["coherence"][COHERENCE_FIELDS[dimension]],
            "justification": head["coherence"]["coherenceJustifications"][dimension]["justification"],
        }
        for dimension in COHERENCE_DIMENSIONS
        if head["coherence"][COHERENCE_FIELDS[dimension]] < base["coherence"][COHERENCE_FIELDS[dimension]]
    ]
    return {
        "tools": tools,
        "minimumTerm": {
            "base": {"tool": base["rollups"]["minTool"], "tdqs": base["rollups"]["minTdqs"]},
            "head": {"tool": head["rollups"]["minTool"], "tdqs": head["rollups"]["minTdqs"]},
        },
        "coherence": coherence,
    }


def _report(
    base_export: dict,
    head_export: dict,
    record: dict,
    decision: str,
    reason: str,
    pairs: list[dict],
    base_commit: str | None,
    head_commit: str | None,
    warnings: Sequence[str] = (),
) -> dict:
    first = _sides(pairs[0]) if pairs else None
    dropped = first is not None and _overall(first[1]) < _overall(first[0])
    return {
        "decision": decision,
        "exitCode": EXIT_CODES[decision],
        "reason": reason,
        "baseCommit": base_commit,
        "headCommit": head_commit,
        "versions": json.loads(json.dumps(record)),
        "versionDigest": version_digest(record),
        "base": {"export": base_export},
        "head": {"export": head_export},
        "size": size_report(base_export, head_export),
        "pairs": pairs,
        "median": medians(pairs) if pairs else None,
        "causes": _causes(*first) if dropped and first is not None else None,
        "confirmation": dict(CONFIRMATION),
        "warnings": list(warnings),
    }


def _reason(decision: str, pairs: Sequence[object]) -> str:
    base, head = (_overall(side) for side in _sides(pairs[0]))
    if len(pairs) == 1:
        return f"The overall score holds: {_decimal(base)} before, {_decimal(head)} after."
    middle = medians(pairs)
    if decision == "block":
        return f"The overall score fell from {_decimal(middle['base'])} to {_decimal(middle['head'])}, the median of each side over three pairs."
    return (
        f"The first pair dropped from {_decimal(base)} to {_decimal(head)}, but the medians over three pairs are "
        f"{_decimal(middle['base'])} before and {_decimal(middle['head'])} after, so the drop is not confirmed."
    )


def report_from_pairs(
    base_export: dict, head_export: dict, record: dict, pairs: Sequence[object], base_commit: str | None = None, head_commit: str | None = None, warnings: Sequence[str] = ()
) -> dict:
    decision = decide(pairs)
    normalized = [dict(zip(("base", "head"), _sides(pair), strict=True)) for pair in pairs]
    return _report(base_export, head_export, record, decision, _reason(decision, pairs), normalized, base_commit, head_commit, warnings)


def compare(base_export: dict, head_export: dict, scorer: Any, record: dict, base_commit: str | None = None, head_commit: str | None = None) -> dict:
    """Score both complete sets and decide; unchanged sets pass without any model call."""
    if base_export["setHash"] == head_export["setHash"]:
        return _report(base_export, head_export, record, "pass", "The tool definitions are unchanged, so nothing was scored.", [], base_commit, head_commit)
    if scorer is None:
        return _report(base_export, head_export, record, "invalid", "The tool definitions changed and there is no model to score them.", [], base_commit, head_commit)
    digest = version_digest(record)
    warnings = list(scorer.warnings()) if callable(getattr(scorer, "warnings", None)) else []
    try:
        result = confirm(lambda: (score_side(base_export, scorer, digest), score_side(head_export, scorer, digest)))
    except InvalidComparison as error:
        return _report(base_export, head_export, record, "invalid", str(error), [], base_commit, head_commit, warnings)
    return report_from_pairs(base_export, head_export, record, result["pairs"], base_commit, head_commit, warnings)


def _json_default(value: object) -> object:
    if isinstance(value, Fraction):
        return float(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def report_json(report: dict) -> str:
    return json.dumps(report, default=_json_default, ensure_ascii=False, indent=2)


def _signed(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)


def _collapsed(text: str) -> str:
    return " ".join(str(text).split())


def summary_markdown(report: dict) -> str:
    lines = [f"## Tool definition score: {report['decision'].upper()}", "", report["reason"], ""]
    if report["pairs"]:
        base, head = _sides(report["pairs"][0])
        b, h = base["rollups"], head["rollups"]

        def scored(value: object) -> str:
            number = _number(value)
            return f"{_decimal(number)} ({tier(number)})"

        lines += [
            "| First pair | Base | Head |",
            "|---|---|---|",
            f"| Overall | {scored(b['overallScore'])} | {scored(h['overallScore'])} |",
            f"| Description quality | {scored(b['descriptionQualityScore'])} | {scored(h['descriptionQualityScore'])} |",
            f"| Coherence | {scored(b['coherenceScore'])} | {scored(h['coherenceScore'])} |",
            f"| Mean TDQS | {_decimal(_number(b['meanTdqs']))} | {_decimal(_number(h['meanTdqs']))} |",
            f"| Minimum TDQS | {_decimal(_number(b['minTdqs']))} `{b['minTool']}` | {_decimal(_number(h['minTdqs']))} `{h['minTool']}` |",
            "",
        ]
        if len(report["pairs"]) > 1:
            lines += ["| Pair | Base overall | Head overall |", "|---|---|---|"]
            for number, pair in enumerate(report["pairs"], 1):
                pair_base, pair_head = _sides(pair)
                lines.append(f"| {number} | {_decimal(_overall(pair_base))} | {_decimal(_overall(pair_head))} |")
            middle = report["median"]
            lines += ["", f"Median overall: {_decimal(_number(middle['base']))} before, {_decimal(_number(middle['head']))} after.", ""]
    causes = report.get("causes")
    if causes:
        lines += ["### What fell on the first pair", ""]
        minimum = causes["minimumTerm"]
        if _number(minimum["head"]["tdqs"]) < _number(minimum["base"]["tdqs"]):
            lines += [
                f"The minimum term fell: `{minimum['head']['tool']}` is the head's minimum at {_decimal(_number(minimum['head']['tdqs']))}, "
                f"against `{minimum['base']['tool']}` at {_decimal(_number(minimum['base']['tdqs']))} on the base. "
                "Description quality weighs the minimum at 40 %.",
                "",
            ]
        if causes["tools"]:
            lines += ["Tools whose TDQS fell, by how much each fall alone lowers the unrounded overall:", "", "| Tool | Base | Head | Effect |", "|---|---|---|---|"]
            for item in causes["tools"]:
                lines.append(f"| `{item['tool']}` | {_decimal(_number(item['base']))} | {_decimal(_number(item['head']))} | {_decimal(_number(item['effect']), 4)} |")
            lines.append("")
            for item in causes["tools"]:
                for dimension in item["dimensions"]:
                    lines.append(f"- `{item['tool']}` {dimension['dimension']} {dimension['base']} to {dimension['head']}: {_collapsed(dimension['justification'])}")
            lines.append("")
        for dimension in causes["coherence"]:
            lines.append(f"- Coherence {dimension['dimension']} {dimension['base']} to {dimension['head']}: {_collapsed(dimension['justification'])}")
        if causes["coherence"]:
            lines.append("")
    if report.get("warnings"):
        lines += ["### Warnings", "", *[f"- {_collapsed(warning)}" for warning in report["warnings"]], ""]
    size = report["size"]
    lines += [
        "### Size (information only, never part of the decision)",
        "",
        "| Measure | Base | Head | Change |",
        "|---|---|---|---|",
        f"| Description characters | {size['base']['descriptionCharacters']} | {size['head']['descriptionCharacters']} | {_signed(size['delta']['descriptionCharacters'])} |",
        f"| tools/list bytes | {size['base']['toolsListBytes']} | {size['head']['toolsListBytes']} | {_signed(size['delta']['toolsListBytes'])} |",
        "",
        "<details><summary>Definition bytes per tool</summary>",
        "",
        "| Tool | Base | Head | Change |",
        "|---|---|---|---|",
    ]
    for name, delta in size["delta"]["definitionBytes"].items():
        lines.append(f"| `{name}` | {size['base']['definitionBytes'].get(name, 0)} | {size['head']['definitionBytes'].get(name, 0)} | {_signed(delta)} |")
    lines += ["", "</details>", ""]
    return "\n".join(lines)


# --- checking a saved report --------------------------------------------------------------------


def _check_export(saved: object, exported: dict, side: str) -> None:
    if not isinstance(saved, dict):
        raise IncompleteResult(f"The report has no {side} export.")
    tools = saved.get("tools")
    if (
        saved.get("setHash") != exported["setHash"]
        or saved.get("hashes") != exported["hashes"]
        or not isinstance(tools, list)
        or {item.get("name"): definition_hash(item) for item in tools if isinstance(item, dict)} != exported["hashes"]
    ):
        raise StaleReport(f"the {side} tool definitions are not the ones it scored.")


def _check_side(side: object, exported: dict, digest: str, label: str) -> None:
    if not isinstance(side, dict):
        raise IncompleteResult(f"A pair has no {label} side.")
    if side.get("setHash") != exported["setHash"]:
        raise StaleReport(f"a {label} side scored another tool set.")
    if side.get("versionDigest") != digest:
        raise StaleReport(f"a {label} side was scored under other versions.")
    results = side.get("tools")
    if not isinstance(results, dict):
        raise IncompleteResult(f"A {label} side has no tool results.")
    names = [item["name"] for item in exported["tools"]]
    missing = [name for name in names if name not in results]
    if missing:
        raise IncompleteResult(f"A {label} side has no result for {', '.join(missing)}.")
    extra = sorted(set(results) - set(names))
    if extra:
        raise InvalidComparison(f"A {label} side scored tools the set does not hold: {', '.join(extra)}.")
    recomputed: dict[str, dict] = {}
    for name in names:
        result = results[name]
        if result.get("definitionHash") != exported["hashes"][name]:
            raise StaleReport(f"the {label} result for {name} scored another definition.")
        scores = result.get("scores")
        if not isinstance(scores, dict) or any(isinstance(scores.get(d), bool) or not isinstance(scores.get(d), int) or not 1 <= scores[d] <= 5 for d in DIMENSIONS):
            raise InvalidComparison(f"The {label} result for {name} has no valid scores.")
        tdqs = compute_tdqs(scores)
        if _number(result.get("tdqs")) != tdqs or result.get("tier") != tier(tdqs):
            raise InvalidComparison(f"The {label} TDQS of {name} does not follow from its scores.")
        recomputed[name] = {"tdqs": tdqs}
    coherence = side.get("coherence")
    if not isinstance(coherence, dict):
        raise IncompleteResult(f"A {label} side has no coherence result.")
    values = {dimension: coherence.get(COHERENCE_FIELDS[dimension]) for dimension in COHERENCE_DIMENSIONS}
    if any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5 for value in values.values()):
        raise IncompleteResult(f"A {label} coherence result lacks a dimension score.")
    score = coherence_score(values)  # type: ignore[arg-type]
    if _number(coherence.get("coherenceScore")) != score or coherence.get("coherenceTier") != tier(score):
        raise InvalidComparison(f"The {label} coherence score does not follow from its dimensions.")
    saved = side.get("rollups")
    if not isinstance(saved, dict):
        raise IncompleteResult(f"A {label} side has no rollups.")
    for key, value in rollups(recomputed, names, {"coherenceScore": score}).items():
        if (key not in saved) or (_number(saved[key]) != value if isinstance(value, Fraction) else saved[key] != value):
            raise InvalidComparison(f"The {label} {key} does not follow from the tool scores.")


def check_report(report: dict, base_export: dict, head_export: dict, record: dict) -> None:
    """Accept a saved report only for the current exports and versions, and only if it holds together."""
    digest = version_digest(record)
    if report.get("versions") != json.loads(json.dumps(record)) or report.get("versionDigest") != digest:
        raise StaleReport("it was made under other versions than the version record.")
    _check_export(report.get("base", {}).get("export") if isinstance(report.get("base"), dict) else None, base_export, "base")
    _check_export(report.get("head", {}).get("export") if isinstance(report.get("head"), dict) else None, head_export, "head")
    decision = report.get("decision")
    if decision not in EXIT_CODES or report.get("exitCode") != EXIT_CODES[decision]:
        raise InvalidComparison("The report's decision and exit code do not match.")
    if decision == "invalid":
        return
    pairs = report.get("pairs")
    if not isinstance(pairs, list):
        raise IncompleteResult("The report has no pairs.")
    if not pairs:
        if base_export["setHash"] != head_export["setHash"]:
            raise IncompleteResult("The definitions changed, but the report holds no scored pair.")
        expected = "pass"
    else:
        for pair in pairs:
            if not isinstance(pair, dict):
                raise IncompleteResult("A pair is not an object.")
            _check_side(pair.get("base"), base_export, digest, "base")
            _check_side(pair.get("head"), head_export, digest, "head")
        expected = decide(pairs)
    if decision != expected:
        raise InvalidComparison(f"The report says {decision}, but its pairs decide {expected}.")


# --- the version record and the calibration ------------------------------------------------------


def load_version_record() -> dict:
    return json.loads(VERSION_RECORD.read_text(encoding="utf-8"))


def load_calibration_record() -> dict:
    return json.loads(CALIBRATION_RECORD.read_text(encoding="utf-8"))


def check_rubric(record: dict) -> None:
    """The record's rubric must be the one this module computes with."""
    expected = {
        "dimension_weights": DIMENSION_WEIGHTS,
        "description_quality_weights": DESCRIPTION_QUALITY_WEIGHTS,
        "overall_weights": OVERALL_WEIGHTS,
        "retries": RETRIES,
        "confirmation": CONFIRMATION,
    }
    for key, value in expected.items():
        if record.get(key) != value:
            raise InvalidComparison(f"The version record's {key} is not the one this gate computes with.")
    thresholds = record.get("tier_thresholds")
    if not isinstance(thresholds, dict) or {letter: _number(value) for letter, value in thresholds.items()} != TIER_THRESHOLDS:
        raise InvalidComparison("The version record's tier_thresholds are not the ones this gate computes with.")


def calibration_summary(registry_tools: dict[str, dict], runs: Sequence[dict[str, dict]]) -> dict:
    """Per tool and per dimension: the median of the runs against the registry's published score."""
    keys = (*DIMENSIONS, "tdqs")
    tools = {}
    for name in sorted(registry_tools):
        published = registry_tools[name]
        registry = {**{d: Fraction(published["scores"][d]) for d in DIMENSIONS}, "tdqs": _number(published["tdqs"])}
        gate = {
            **{d: _median([Fraction(run[name]["scores"][d]) for run in runs]) for d in DIMENSIONS},
            "tdqs": _median([_number(run[name]["tdqs"]) for run in runs]),
        }
        tools[name] = {"registry": registry, "gate": gate, "difference": {key: gate[key] - registry[key] for key in keys}}
    count = len(tools)
    mean_absolute = {key: sum((abs(item["difference"][key]) for item in tools.values()), Fraction(0)) / count for key in ("tdqs", *DIMENSIONS)}
    largest = sorted(tools, key=lambda name: (-abs(tools[name]["difference"]["tdqs"]), name))[:10]
    return {"tools": tools, "meanAbsoluteDifference": mean_absolute, "largestDifferences": [{"tool": name, **tools[name]} for name in largest]}


def _camel_to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def registry_capture(path: Path) -> tuple[dict, list[dict]]:
    """A capture of the registry's published scores: {"server": {...}, "tools": [{name, definition, qualityScore}]}."""
    capture = json.loads(Path(path).read_text(encoding="utf-8"))
    published: dict[str, dict] = {}
    definitions = []
    for item in capture["tools"]:
        quality = item["qualityScore"]
        published[item["name"]] = {"scores": {_camel_to_snake(key): quality[key] for key in quality if _camel_to_snake(key) in DIMENSIONS}, "tdqs": quality["tdqs"], "tier": quality.get("tier")}
        definitions.append(item["definition"])
    return {"server": capture.get("server", {}), "tools": published}, definitions


def _run_record(side: dict) -> dict:
    coherence = side["coherence"]
    return json.loads(
        report_json(
            {
                "tools": {name: {"scores": result["scores"], "tdqs": result["tdqs"]} for name, result in side["tools"].items()},
                "coherence": {**{dimension: coherence[COHERENCE_FIELDS[dimension]] for dimension in COHERENCE_DIMENSIONS}, "coherenceScore": coherence["coherenceScore"]},
                "rollups": side["rollups"],
            }
        )
    )


# --- the export: what a host sees over stdio ------------------------------------------------------

# Imports the package from the given src and checks that it came from there, so
# an installed copy can never stand in for the revision being exported.
BOOTSTRAP = "\n".join(
    [
        "import os, runpy, sys",
        "src = os.path.abspath(sys.argv[1])",
        "sys.path.insert(0, src)",
        "import agentic_hil",
        "origin = os.path.normcase(os.path.abspath(agentic_hil.__file__))",
        "if not origin.startswith(os.path.join(os.path.normcase(src), '')):",
        "    sys.exit('agentic_hil was imported from ' + agentic_hil.__file__ + ', not from ' + src)",
        "sys.argv = ['agentic_hil', 'mcp-stdio']",
        "runpy.run_module('agentic_hil', run_name='__main__', alter_sys=True)",
    ]
)
REDIRECTED_DIRECTORIES = ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "HOME", "USERPROFILE")


def run_server(argv: list[str], cwd: Path | str, env: dict[str, str], stdin_text: str, timeout: float) -> tuple[int, str, str]:
    try:
        done = subprocess.run(argv, input=stdin_text.encode("utf-8"), capture_output=True, cwd=cwd, env=env, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise InvalidComparison(f"The server did not answer tools/list within {timeout:.0f} s.") from None
    return done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace")


def _server_environment(home: Path) -> dict[str, str]:
    """No Agentic HIL configuration, no user site, no inherited Python path: a fresh host."""
    env = {name: value for name, value in os.environ.items() if not (name.upper().startswith(("AGENTIC_HIL_", "GIT_")) or name.upper() == "PYTHONPATH")}
    env["PYTHONNOUSERSITE"] = "1"
    for name in REDIRECTED_DIRECTORIES:
        env[name] = str(home / name.lower())
    return env


def export_tools(src: Path | str) -> dict:
    """tools/list from the server in `src`, started over stdio with no configuration, as the registry starts it."""
    src = Path(src).resolve()
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "tool-definition-score", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    with tempfile.TemporaryDirectory(prefix="tdqs-export-") as scratch:
        home, work = Path(scratch) / "home", Path(scratch) / "work"
        home.mkdir()
        work.mkdir()
        returncode, stdout, stderr = run_server([sys.executable, "-c", BOOTSTRAP, str(src)], work, _server_environment(home), "".join(json.dumps(message) + "\n" for message in messages), SERVER_TIMEOUT_S)
    responses = {}
    for line in stdout.splitlines():
        if line.strip():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and "id" in message:
                responses[message["id"]] = message
    if 1 not in responses or 2 not in responses:
        tail = stderr.strip().splitlines()[-1:] or [f"exit code {returncode}"]
        raise InvalidComparison(f"The server in {src} did not answer initialize and tools/list: {tail[0]}")
    for message in (responses[1], responses[2]):
        if "error" in message:
            raise InvalidComparison(f"The server in {src} answered with an error: {message['error']}")
    listing = responses[2]["result"]
    if listing.get("nextCursor"):
        raise InvalidComparison("tools/list is paginated; this gate reads a single page.")
    info = responses[1]["result"].get("serverInfo", {})
    return export_from_tools(listing["tools"], server_name=info.get("name", ""), server_version=info.get("version", ""))


def _git(repo: Path, *args: str) -> str:
    env = {name: value for name, value in os.environ.items() if not name.upper().startswith("GIT_")}
    done = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False)
    if done.returncode != 0:
        raise InvalidComparison(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def rev_parse(repo: Path | str, ref: str) -> str:
    return _git(Path(repo), "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def export_revision(repo: Path | str, ref: str) -> dict:
    """tools/list of a revision: a detached worktree of it, exported with that tree's own src.

    The worktree runs with the interpreter and dependencies of the current
    environment; only the package itself comes from the revision."""
    repo = Path(repo).resolve()
    scratch = Path(tempfile.mkdtemp(prefix="tdqs-base-"))
    tree = scratch / "tree"
    try:
        _git(repo, "worktree", "add", "--detach", str(tree), ref)
        try:
            return export_tools(tree / "src")
        finally:
            try:
                _git(repo, "worktree", "remove", "--force", str(tree))
            finally:
                _git(repo, "worktree", "prune")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --- the model backend: the Claude Code command line ---------------------------------------------

# What the command line would otherwise add to the request or do around it.
CLI_ENVIRONMENT = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_THINKING": "1",
    "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
    "CLAUDE_CODE_CARVED_SLATE": "0",
    "CLAUDE_CODE_TOTAL_TOKENS_REMINDER": "off",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
}
# Kept for a developer's own login; every other CLAUDE* variable is dropped.
LOCAL_LOGIN_VARIABLES = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")
SECRET_VARIABLES = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class ClaudeCliScorer:
    """Asks the pinned model through `claude -p`: the system prompt, and the rendered prompt on stdin.

    No tools, no MCP servers, no settings, no project memory, no session kept,
    and a fresh empty working directory per call. With `token_env` the token
    from that variable is the only credential and each call gets its own empty
    configuration directory; without it the developer's own login is used.

    The command line adds three things no flag removes, captured from the
    request it sends: a billing line and one SDK identity sentence ahead of
    the system prompt, and a reminder with the current date ahead of the user
    message, which under a personal login also names the account's email
    address. They are the same for both sides of a comparison."""

    def __init__(
        self,
        prompts: dict[str, str],
        record: dict,
        command: Sequence[str] | None = None,
        environ: dict[str, str] | None = None,
        token_env: str | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        timeout: float = CLI_TIMEOUT_S,
    ) -> None:
        self.prompts = dict(prompts)
        self.model = record["model"]
        self.cli_version = record["cli_version"]
        self.environ = dict(os.environ if environ is None else environ)
        self.token_env = token_env
        self.token: str | None = None
        if token_env:
            self.token = self.environ.get(token_env) or None
            if self.token is None:
                raise BackendError(f"{token_env} is not set.")
        self.command = list(command) if command else self._find_command()
        self.concurrency = concurrency
        self.timeout = timeout
        self.calls = 0
        self._slots = threading.BoundedSemaphore(concurrency)
        self._lock = threading.Lock()

    def _find_command(self) -> list[str]:
        found = shutil.which("claude", path=self.environ.get("PATH"))
        if found is None:
            raise BackendError("The claude command is not on PATH; install the pinned Claude Code version.")
        if os.name == "nt" and Path(found).suffix.lower() in (".cmd", ".bat"):
            raise BackendError(f"{found} is a batch file, which cannot pass the system prompt intact; use the native claude executable.")
        return [found]

    def _redact(self, text: str) -> str:
        for secret in {self.token, *(self.environ.get(name) for name in SECRET_VARIABLES)}:
            if secret and len(secret) >= 8:
                text = text.replace(secret, "[redacted]")
        return text

    def _environment(self, config_dir: str | None) -> dict[str, str]:
        env = {}
        for name, value in self.environ.items():
            upper = name.upper()
            if name == self.token_env:
                continue
            if upper.startswith("CLAUDE"):
                if self.token is None and upper in LOCAL_LOGIN_VARIABLES:
                    env[name] = value
                continue
            if upper.startswith("ANTHROPIC_") and self.token is not None:
                continue
            env[name] = value
        env.update(CLI_ENVIRONMENT)
        if self.token is not None:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self.token
            env["CLAUDE_CONFIG_DIR"] = config_dir or ""
        return env

    def _launch(self, argv: list[str], stdin: bytes, timeout: float) -> subprocess.CompletedProcess:
        workdir = tempfile.mkdtemp(prefix="tdqs-call-")
        config_dir = tempfile.mkdtemp(prefix="tdqs-config-") if self.token is not None else None
        try:
            return subprocess.run(argv, input=stdin, capture_output=True, cwd=workdir, env=self._environment(config_dir), timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise BackendError(f"The model call did not finish within {timeout:.0f} s.") from None
        except OSError as error:
            raise BackendError(self._redact(f"The claude command could not start: {error}")) from None
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            if config_dir is not None:
                shutil.rmtree(config_dir, ignore_errors=True)

    def _call(self, system: str, prompt: str) -> str:
        argv = [
            *self.command,
            "-p",
            "--model", self.model,
            "--system-prompt", system,
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--safe-mode",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--output-format", "json",
        ]
        with self._slots:
            with self._lock:
                self.calls += 1
            done = self._launch(argv, prompt.encode("utf-8"), self.timeout)
        if done.returncode != 0:
            tail = " ".join(done.stderr.decode("utf-8", "replace").strip().splitlines()[-3:])
            raise BackendError(self._redact(f"claude exited with {done.returncode}: {tail}"))
        try:
            envelope = json.loads(done.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BackendError("claude did not answer with its JSON envelope.") from None
        if not isinstance(envelope, dict):
            raise BackendError("claude did not answer with its JSON envelope.")
        if envelope.get("is_error"):
            raise BackendError(self._redact(f"claude reported an error: {str(envelope.get('result'))[:300]}"))
        usage = envelope.get("modelUsage")
        if not isinstance(usage, dict) or set(usage) != {self.model}:
            raise BackendError(f"claude answered with {sorted(usage) if isinstance(usage, dict) else usage!r}, not only {self.model}.")
        if not isinstance(envelope.get("result"), str):
            raise BackendError("claude's envelope carries no result text.")
        return envelope["result"]

    def tool_answer(self, definition: dict, sibling_names: Sequence[str]) -> str:
        return self._call(self.prompts["tool_system"], render_tool_prompt(self.prompts["tool_user"], definition, list(sibling_names)))

    def coherence_answer(self, server_name: str, tools: Sequence[dict], candidates: Sequence[dict]) -> str:
        return self._call(self.prompts["coherence_system"], render_coherence_prompt(self.prompts["coherence_user"], server_name, tools, candidates))

    def warnings(self) -> list[str]:
        """A CLI version other than the recorded one is reported; it does not count as a model call."""
        try:
            done = self._launch([*self.command, "--version"], b"", VERSION_TIMEOUT_S)
            found = re.search(r"\d+\.\d+\.\d+", done.stdout.decode("utf-8", "replace"))
        except BackendError as error:
            return [f"The Claude Code version could not be read: {error}"]
        installed = found.group(0) if found else "unknown"
        if installed != self.cli_version:
            return [f"Claude Code {installed} is installed, but the scores were calibrated with {self.cli_version}."]
        return []


# --- the entry point ---------------------------------------------------------------------------------


def make_scorer(prompts: dict[str, str], record: dict, args: argparse.Namespace, environ: dict[str, str]) -> ClaudeCliScorer:
    token_env = args.token_env if args.token_env and environ.get(args.token_env) else None
    return ClaudeCliScorer(prompts, record, environ=environ, token_env=token_env, concurrency=args.concurrency)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Keep the overall Tool Definition Quality Score from falling.")
    parser.add_argument("--base", help="the base revision, for example origin/master")
    parser.add_argument("--head", help="a head revision; the default is the working tree")
    parser.add_argument("--repo", default=".", help="the repository root (default: the current directory)")
    parser.add_argument("--report-dir", default=".tool-definition-score", help="where the JSON report and its Markdown summary go")
    parser.add_argument("--token-env", help="the variable holding a Claude Code OAuth token; without it the local login is used")
    parser.add_argument("--check", metavar="REPORT", help="check a saved report against the current exports instead of scoring")
    parser.add_argument("--cache-dir", help="where the fetched specification is cached (default: the user cache directory)")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="model calls at once (default: %(default)s)")
    parser.add_argument("--calibrate", metavar="REGISTRY_JSON", help="score the head several times against a capture of the registry's published scores")
    parser.add_argument("--runs", type=int, default=3, help="scoring runs for --calibrate (default: %(default)s)")
    return parser


def _append(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def _write_report(report: dict, report_dir: Path, environ: dict[str, str]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    markdown = summary_markdown(report)
    (report_dir / REPORT_JSON).write_text(report_json(report) + "\n", encoding="utf-8")
    (report_dir / REPORT_MARKDOWN).write_text(markdown, encoding="utf-8")
    _append(environ.get("GITHUB_STEP_SUMMARY"), markdown + "\n")


def _calibrate(args: argparse.Namespace, record: dict, environ: dict[str, str], repo: Path) -> int:
    registry, definitions = registry_capture(Path(args.calibrate))
    commit = rev_parse(repo, args.head or "HEAD")
    exported = export_revision(repo, commit) if args.head else export_tools(repo / "src")
    if set_hash(definitions) != exported["setHash"]:
        diff = diff_definitions(definitions, exported["tools"])
        raise InvalidComparison(f"The registry scored other definitions than {commit}: added {diff.added}, removed {diff.removed}, changed {diff.changed}.")
    prompts = load_prompts(record, Path(args.cache_dir) if args.cache_dir else default_cache_dir(environ), fetch_url)
    scorer = make_scorer(prompts, record, args, environ)
    digest = version_digest(record)
    runs = [_run_record(score_side(exported, scorer, digest)) for _ in range(args.runs)]
    calibration = {
        "version_digest": digest,
        "registry": {"source": "https://glama.ai/mcp/servers/agentic-hil/agentic-hil", **registry, "setHash": set_hash(definitions)},
        "evaluated": {"commit": commit, "setHash": exported["setHash"], "toolCount": len(exported["tools"])},
        "runs": runs,
        "summary": json.loads(report_json(calibration_summary(registry["tools"], [run["tools"] for run in runs]))),
    }
    target = Path(args.report_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / "calibration.json").write_text(json.dumps(calibration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"CALIBRATED: {args.runs} runs, {scorer.calls} model calls, mean absolute difference {calibration['summary']['meanAbsoluteDifference']['tdqs']:.2f} on the TDQS.")
    return 0


def main(argv: Sequence[str] | None = None, environ: dict[str, str] | None = None) -> int:
    environ = dict(os.environ) if environ is None else dict(environ)
    args = _parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    report_dir = Path(args.report_dir)
    scorer = None
    exports: tuple[dict, dict] | None = None
    record: dict | None = None
    try:
        record = load_version_record()
        check_rubric(record)
        if args.calibrate:
            return _calibrate(args, record, environ, repo)
        if not args.base:
            raise InvalidComparison("--base is required.")
        base_commit = rev_parse(repo, args.base)
        base_export = export_revision(repo, base_commit)
        if args.head:
            head_commit = rev_parse(repo, args.head)
            head_export = export_revision(repo, head_commit)
        else:
            head_commit = rev_parse(repo, "HEAD")
            head_export = export_tools(repo / "src")
        exports = (base_export, head_export)
        if args.check:
            saved = json.loads(Path(args.check).read_text(encoding="utf-8"))
            try:
                check_report(saved, base_export, head_export, record)
            except StaleReport as error:
                print(f"INVALID: The saved report is stale: {error}")
                return EXIT_CODES["invalid"]
            print(f"{saved['decision'].upper()}: {saved['reason']}")
            return EXIT_CODES[saved["decision"]]
        if base_export["setHash"] != head_export["setHash"]:
            if args.token_env and not environ.get(args.token_env):
                reason = f"The tool definitions changed and {args.token_env} is not set, so they cannot be scored."
                report = _report(base_export, head_export, record, "invalid", reason, [], base_commit, head_commit)
            else:
                prompts = load_prompts(record, Path(args.cache_dir) if args.cache_dir else default_cache_dir(environ), fetch_url)
                scorer = make_scorer(prompts, record, args, environ)
                report = compare(base_export, head_export, scorer, record, base_commit, head_commit)
        else:
            report = compare(base_export, head_export, None, record, base_commit, head_commit)
        _write_report(report, report_dir, environ)
        print(f"{report['decision'].upper()}: {report['reason']}")
        return report["exitCode"]
    except Exception as error:  # every failure is a decisive INVALID line, never a traceback alone
        message = str(error) or type(error).__name__
        print(f"INVALID: {message}")
        if exports is not None and record is not None and not args.check:
            try:
                _write_report(_report(*exports, record, "invalid", message, [], None, None), report_dir, environ)
            except Exception as write_error:  # noqa: BLE001 - the INVALID line above is the result
                print(f"INVALID: the report could not be written either: {write_error}")
        return EXIT_CODES["invalid"]
    finally:
        if not args.check and not args.calibrate:
            _append(environ.get("GITHUB_OUTPUT"), f"scored={'true' if scorer is not None else 'false'}\n")


if __name__ == "__main__":
    sys.exit(main())
