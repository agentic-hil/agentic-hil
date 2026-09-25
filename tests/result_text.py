"""The text block of a `tools/call` result, held to the document it is taken from.

A result goes out twice. `structuredContent` is the whole document, for the
hosts and programs that read it. The content text block is what an agent host
puts into the model's context, so it is a compact projection of the same
document: a key whose value is null, "", [] or {} is left out at any depth, and
so is a nested object left with no keys once its own empty keys are; nine
top-level fields are left out where they only restate their default; and a
top-level remediation or likely_causes entry the server already sent in this
session is left out, counted under `repeated_advice`, with `advice_uri` naming
the catalogue entry that serves it whole.

Which advice a live server has already sent depends on everything earlier in
the session, which the tiers that drive one do not track call by call. So this
check is exact about everything else and tolerant about exactly that: each
top-level advice list in the text is the document's list with some entries left
out and the rest in their order, and the number left out is the number
`repeated_advice` gives.
"""

from __future__ import annotations

import json
from typing import Any

# The top-level pairs the text leaves out because they only restate a default.
# The same field holding any other value is kept, and so is every field not
# named here, `ok` and `retry_safe` included.
DEFAULTS: dict[str, object] = {
    "side_effect_committed": False,
    "side_effect_status": "not_started",
    "hardware_state": "unchanged",
    "cleanup_required": False,
    "quarantined": False,
    "audit_ok": True,
    "cleanup_ok": True,
    "target_ok": True,
    "config_stale": False,
}

# The top-level lists whose entries a session is sent once. `quarantine_guidance`
# is not one of them: no resource serves it again, so it is always sent whole.
ADVICE_FIELDS = ("remediation", "likely_causes")

ERRORS_URI_PREFIX = "agentic-hil://reference/errors/"


def same(expected: Any, actual: Any) -> bool:
    """Equal and of the same JSON type: `true` is not `1`, which `==` would allow."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and expected.keys() == actual.keys() and all(same(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(same(value, other) for value, other in zip(expected, actual, strict=True))
    return type(expected) is type(actual) and expected == actual


def is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, (str, list, dict)) and not value)


def is_default(key: str, value: Any) -> bool:
    return key in DEFAULTS and same(DEFAULTS[key], value)


def vanishes(value: Any) -> bool:
    """Whether leaving out its empty keys would leave nothing of this object."""
    return isinstance(value, dict) and all(is_empty(child) or vanishes(child) for child in value.values())


def projects(value: Any, sent: Any) -> bool:
    """Whether ``sent`` is ``value`` with its empty keys left out, at any depth.

    A key is left out when its value is empty, or is an object with no keys left
    once its own empty keys are. An array keeps every element in its place: an
    object inside one has its own empty keys left out and is sent as {} when none
    are left, and any other element is sent as it is."""
    if isinstance(value, dict):
        if not isinstance(sent, dict) or not set(sent) <= set(value):
            return False
        for key, child in value.items():
            if is_empty(child) or vanishes(child):
                if key in sent:
                    return False
            elif key not in sent or not projects(child, sent[key]):
                return False
        return True
    if isinstance(value, list):
        return isinstance(sent, list) and len(sent) == len(value) and all(projects(child, other) for child, other in zip(value, sent, strict=True))
    return same(value, sent)


def in_order(kept: list, entries: list) -> bool:
    """Whether ``kept`` is ``entries`` with some of them left out and the rest in their order."""
    remaining = iter(entries)
    return all(any(projects(entry, candidate) for entry in remaining) for candidate in kept)


def text_document(result: dict) -> dict:
    """The one text block of a `tools/call` result, parsed, after checking it is one compact JSON object."""
    content = result.get("content")
    assert isinstance(content, list) and len(content) == 1, f"a result carries exactly one content block: {result}"
    block = content[0]
    assert isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str), f"the content block is text: {result}"
    text = block["text"]
    document = json.loads(text)
    assert isinstance(document, dict), f"the text block is one JSON object: {text}"
    compact = {json.dumps(document, separators=(",", ":")), json.dumps(document, separators=(",", ":"), ensure_ascii=False)}
    assert text in compact, f"the text block is serialized without whitespace: {text}"
    return document


def assert_text_projects(result: dict) -> dict:
    """Hold a `tools/call` result's text block to the projection of its structuredContent, and return it parsed."""
    sent = text_document(result)
    text = result["content"][0]["text"]
    # Through JSON once, so the document compares the way the wire carries it.
    document = json.loads(json.dumps(result["structuredContent"]))
    assert isinstance(document, dict), f"structuredContent is one JSON object: {result}"
    for key in ("ok", "tool"):
        if key in document:
            assert key in sent and same(document[key], sent[key]), f"`{key}` is always kept as it is: {text}"
    extra = set(sent) - set(document) - {"repeated_advice", "advice_uri"}
    assert not extra, f"the text carries keys the document has not got, {sorted(extra)}: {text}"
    repeated = sent.get("repeated_advice", {})
    if "repeated_advice" in sent:
        assert isinstance(repeated, dict) and repeated, f"`repeated_advice` is there only when advice was left out: {text}"
        for field, count in repeated.items():
            assert field in ADVICE_FIELDS and isinstance(document.get(field), list), f"`repeated_advice` counts advice fields the document carries: {text}"
            assert type(count) is int and count > 0, f"`repeated_advice` counts the entries left out: {text}"
    for key, value in document.items():
        if key in ("ok", "tool"):
            continue
        if is_empty(value) or is_default(key, value) or vanishes(value):
            assert key not in sent, f"`{key}` is empty or restates its default and is still in the text: {text}"
        elif key in ADVICE_FIELDS and isinstance(value, list):
            kept = sent.get(key, [])
            assert key not in sent or (isinstance(kept, list) and kept), f"`{key}` is left out of the text when all its entries are: {text}"
            assert in_order(kept, value), f"`{key}` in the text is the document's entries, some left out, in their order: {text}"
            left_out = len(value) - len(kept)
            assert left_out == repeated.get(key, 0), f"`{key}` left {left_out} entries out and `repeated_advice` counts {repeated.get(key, 0)}: {text}"
        else:
            assert key in sent and projects(value, sent[key]), f"`{key}` is the document's value with its empty keys left out: {text}"
    if "advice_uri" in sent:
        uri = sent["advice_uri"]
        entry = f"{ERRORS_URI_PREFIX}{document.get('error_type')}"
        assert repeated, f"`advice_uri` comes with advice left out: {text}"
        assert isinstance(uri, str) and (uri == entry or (uri.startswith(f"{entry}:") and len(uri) > len(entry) + 1)), f"`advice_uri` names the catalogue entry of this result's error_type: {text}"
    return sent
