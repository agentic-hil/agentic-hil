"""What `flash_firmware`, `artifact_upload`, `get_last_report` and `classify_last_error` tell an agent (#640).

The four definitions said what the tools are and little else: nothing about
the permission a flash needs, what a debug session on the probe does to it,
where an image may come from, what bounds the call or what happens to the
board when it fails; nothing about how an uploaded artifact is named; and
nothing about what the two report tools read, when they find nothing, or that
the report they return can be another call's. These tests ask the definitions a
host receives through `tools/list` to say those things, and every claim they
ask for is first shown to be what the code does, through `tools/call`, against
the fake debuggers. No probe, port or board is touched.

The metadata tests check meaning, not wording. A claim is checked as a
relation inside one sentence or clause (the refusal and the permission that
produces it, the default and its value, the record and what it outlives), and
the inverted claim an agent could act on wrongly is rejected outright. The
relation checks are run twice more: against paraphrases they must accept and
inversions they must reject, and against the listed definitions with one
meaning flipped (true and false, needed and optional, and and or, an effect
and its negation), which each check must then refuse. Every identifier a
definition names, at any depth of its schema, is one the server lists,
configures or can answer with.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import pathlib
import re
import time
from pathlib import Path

import pytest
from conftest import (
    DEFAULT_TEST_PERMISSIONS,
    FAKE_OPENOCD,
    FAKE_OPENOCD_ERASE_REFUSED,
    FAKE_OPENOCD_NO_TARGET,
    FAKE_PYOCD,
    write_config,
)
from test_debug_sessions import START_TIMEOUT_S, debug_service
from test_debugger_processes import CALL_CEILING_S, FAKE_HUNG_DEBUGGER, config_with_debugger
from test_mcp_reference_resources import read_text
from test_read_until import close
from test_tool_definition_uart import (
    NEGATION,
    QUOTED,
    SNAKE_CASE,
    answer_vocabulary,
    claims,
    clauses,
    definition_text,
    denied,
    denies,
    names,
    one_of,
    property_text,
    sentences,
    stated,
)

import agentic_hil
import agentic_hil.report as report_module
from agentic_hil.bench import BenchMutex
from agentic_hil.config import load_config
from agentic_hil.devices import debugger_device
from agentic_hil.knowledge import ERROR_CATALOGUE, ERROR_URI_PREFIX
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.report import overall_success, report_state_path
from agentic_hil.tools import AgenticHILToolService

FLASH = "flash_firmware"
UPLOAD = "artifact_upload"
LAST_REPORT = "get_last_report"
CLASSIFY = "classify_last_error"
TOOLS = (FLASH, UPLOAD, LAST_REPORT, CLASSIFY)
REPORT_TOOLS = (LAST_REPORT, CLASSIFY)
DESCRIPTION_LIMIT = 400
PROPERTY_DESCRIPTION_LIMIT = 200

# The defaults the code applies when the configuration leaves a key out
# (src/agentic_hil/config.py): `debuggers.<name>.timeout_s` and
# `artifacts.max_upload_size_mb`, `artifacts.allowed_roots`,
# `artifacts.allowed_extensions`.
DEFAULT_DEBUGGER_TIMEOUT_S = 60
DEFAULT_MAX_UPLOAD_SIZE_MB = 64
DEFAULT_ALLOWED_ROOT = "build"
DEFAULT_EXTENSIONS = (".elf", ".hex", ".bin")
# What `write_config` grants a test configuration: one MiB.
TEST_MAX_UPLOAD_BYTES = 1024 * 1024
# The capture's text limits and its wait (src/agentic_hil/readuntil.py).
UNTIL_MAX_ENTRIES = 8
UNTIL_MAX_CHARACTERS = 256
CAPTURE_DEFAULT_WAIT_S = 10
CAPTURE_MAX_WAIT_S = 60

# The smallest image the validator accepts as an ELF: the magic, then padding.
ELF = b"\x7fELF" + b"\x00" * 60
IMAGE = "build/app.elf"
BINARY = "build/app.bin"
UNKNOWN_ID = "0" * 64 + ".elf"

# A pyOCD whose flash succeeds and whose post-flash reset fails (#506).
FAKE_PYOCD_RESET_REFUSED = Path(__file__).resolve().parent / "fixtures" / "fake_pyocd_reset_refused.py"
# fake_pyocd.py behind a delay on each command that drives the target, so a
# flash with a reset runs two slow commands in one call.
SLOW_PYOCD = """import runpy, sys, time
if sys.argv[1:2] in (["flash"], ["commander"]):
    time.sleep({delay_s})
runpy.run_path({fake!r}, run_name="__main__")
"""


# ---------------------------------------------------------------------------
# Services and calls.


def new_service(workspace: Path, **kwargs: object) -> AgenticHILToolService:
    """A server on the fake OpenOCD, with `build/app.elf` and `build/app.bin` in its workspace."""
    path = write_config(workspace, **kwargs)
    (workspace / "build").mkdir(parents=True, exist_ok=True)
    (workspace / IMAGE).write_bytes(ELF)
    (workspace / BINARY).write_bytes(b"\x01" * 64)
    return AgenticHILToolService(load_config(str(path)), frontend="mcp")


def edited_service(workspace: Path, edit, **kwargs: object) -> AgenticHILToolService:
    """A server whose written configuration was edited the way an operator edits the file."""
    path = write_config(workspace, **kwargs)
    path.write_text(edit(path.read_text(encoding="utf-8")), encoding="utf-8")
    (workspace / "build").mkdir(parents=True, exist_ok=True)
    (workspace / IMAGE).write_bytes(ELF)
    return AgenticHILToolService(load_config(str(path)), frontend="mcp")


def without_debuggers(text: str) -> str:
    """The same configuration with no debugger configured at all."""
    edited, count = re.subn(r"(?m)^debuggers:\n(?:[ \t]+.*\n)+", "debuggers: {}\n", text)
    assert count == 1, text
    return edited


def without_uploads(text: str) -> str:
    assert text.count("allow_upload: true") == 1, text
    return text.replace("allow_upload: true", "allow_upload: false")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def call(service: AgenticHILToolService, name: str, arguments: dict) -> dict:
    """One `tools/call`, answered with the structured result an agent acts on.

    The envelope's `isError` is the server's whole verdict, `overall_success`
    (src/agentic_hil/mcp.py), not `ok` alone: a flash whose report could not be
    written answers `ok: true` and is still an error."""
    response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}, service)
    assert isinstance(response, dict) and "result" in response, response
    answer = response["result"]
    structured = answer["structuredContent"]
    assert answer["isError"] is (not overall_success(structured)), answer
    return structured


def flash(service: AgenticHILToolService, **arguments: object) -> dict:
    return call(service, FLASH, arguments if arguments else {"image_path": IMAGE})


def upload(service: AgenticHILToolService, **arguments: object) -> dict:
    return call(service, UPLOAD, arguments)


def last_report(service: AgenticHILToolService) -> dict:
    return call(service, LAST_REPORT, {})


def classify(service: AgenticHILToolService) -> dict:
    return call(service, CLASSIFY, {})


# ---------------------------------------------------------------------------
# What the definitions say, read the way a host reads them.


@pytest.fixture
def listed(tmp_path: Path) -> dict[str, dict]:
    service = new_service(tmp_path / "listed")
    try:
        response = handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, service)
    finally:
        close(service)
    assert isinstance(response, dict), response
    return {str(tool["name"]): tool for tool in response["result"]["tools"]}


def text_of(listed: dict[str, dict], name: str) -> str:
    return definition_text(listed[name])


def described_schemas(schema: dict, path: tuple[str, ...] = ()):
    """Every property schema at any depth of `schema`, with the path to it:
    nested objects, array items and each branch of oneOf, anyOf and allOf."""
    for key, child in (schema.get("properties") or {}).items():
        yield (*path, key), child
        yield from described_schemas(child, (*path, key))
    items = schema.get("items")
    if isinstance(items, dict):
        yield from described_schemas(items, (*path, "items"))
    for keyword in ("oneOf", "anyOf", "allOf"):
        for index, branch in enumerate(schema.get(keyword) or []):
            if isinstance(branch, dict):
                yield from described_schemas(branch, (*path, f"{keyword}[{index}]"))


def nested_text(tool: dict, *path: str) -> str:
    node = tool["inputSchema"]
    for key in path:
        node = node["properties"][key]
    return str(node.get("description") or "")


def every_description(tool: dict) -> str:
    """The description and every property description at any depth."""
    return " ".join([str(tool.get("description") or ""), *(str(child.get("description") or "") for _, child in described_schemas(tool["inputSchema"]))])


# The relations, each a predicate over a definition's text, so the paraphrase
# and mutation tests at the end of this section can run them on texts of their
# own.

DEFAULT_FALSE = r"\bdefaults?\b[^.;]*\bfalse\b|\bfalse\b[^.;]*\bdefault\b"
DEFAULT_TRUE = r"\bdefaults?\b[^.;]*\btrue\b|\btrue\b[^.;]*\bdefault\b"
# The result named `timeout`, not the configuration key `timeout_s`.
TIMEOUT_RESULT = r"(?<![A-Za-z0-9_])timeout(?![A-Za-z0-9_])"
# The outer `ok`, not `report.ok`.
OK_WORD = r"(?<![A-Za-z0-9_.])ok(?![A-Za-z0-9_.])"
NEGATED_NEED = r"\b(?:needs?|requires?)\s+no\b|\b(?:does\s+not|doesn't|never)\s+(?:need|require)|\bnot\s+(?:needed|required)\b|\boptional\b"
BACKENDS = r"(?:OpenOCD|pyOCD|STM32CubeProgrammer)"


def needs_with_refusal(text: str, key: str, refusal: str = "permission_denied") -> bool:
    """`key` is needed and its absence answers `refusal`, as one relation in one
    clause; never "needs no `key`", and never `refusal` for `key` granted."""
    named = rf"(?:[\w<>]+\.)*{key}\b"
    relation = (
        rf"\b(?:needs?|requires?)\s+{named}[^.;]*\b(?:else|otherwise)\b[^.;]*\b{refusal}\b"
        rf"|\bwithout\s+{named}[^.;]*\b{refusal}\b"
        rf"|\b{key}\s+(?:off|false|unset|missing|not granted)\b[^.;]*\b{refusal}\b"
        rf"|\b{refusal}\b[^.;]*\b(?:unless|until)\b[^.;]*\b{key}\b"
    )
    granted = rf"\b{key}\s+(?:is\s+)?(?:true|on|granted|set|given)\b[^.;]*\b{refusal}\b"
    waived = any(re.search(rf"\b{key}\b", part) and re.search(NEGATED_NEED, part, re.IGNORECASE) for part in clauses(text))
    related = any(re.search(relation, part, re.IGNORECASE) for part in clauses(text))
    return related and not waived and not claims(text, granted)


def says_debug_session_makes_flash_busy(text: str) -> bool:
    """A debug session on the probe answers resource_busy until debug_stop_session; the flash never ends it itself."""
    busy = one_of(clauses(text), r"\bresource_busy\b", r"\bdebug_stop_session\b|\bdebug session\b")
    takes_over = claims(text, r"\b(?:ends|stops|closes|replaces|takes over)\s+(?:the|a|any)\s+(?:active\s+|running\s+)?debug session")
    return busy and not takes_over


# The reset as a verb or a noun, not the key `reset_after_flash`.
RESET = r"\breset(?:s|ting)?\b"


def true_denies_reset(text: str) -> bool:
    """Some clause says `true` does not reset: a reset after `true` that a negation reaches."""
    return any(denied(RESET, part[found.end() :]) for part in clauses(text) for found in re.finditer(r"\btrue\b", part, re.IGNORECASE))


def says_reset_default_is_false(text: str) -> bool:
    """reset_after_flash defaults to false and false does not reset; never a true default, never "true does not reset".
    The negation has to reach the reset: "the board is reset and nothing is verified" resets it."""
    default = one_of(clauses(text), DEFAULT_FALSE)
    unreset = any(
        re.search(r"\bfalse\b|\bdefault\b", part, re.IGNORECASE)
        and (denied(RESET, part) or re.search(r"\bunreset\b|\breset\s+(?:is\s+not|isn't|does\s+not|doesn't|never)\b", part, re.IGNORECASE))
        for part in clauses(text)
    )
    return default and unreset and not claims(text, DEFAULT_TRUE) and not true_denies_reset(text)


def says_true_resets(text: str) -> bool:
    """reset_after_flash resets the board once the flash is done: a clause that states the reset, not one that denies it.
    Only a negation that reaches the reset denies it: "true resets the board after flash and doesn't verify it" resets."""
    affirmed = any(
        stated(RESET, part)
        and re.search(r"\bflash", part, re.IGNORECASE)
        and re.search(r"\bafter\b|\bthen\b|\bonce\b", part, re.IGNORECASE)
        and not re.search(r"\bfail", part, re.IGNORECASE)
        for part in clauses(text)
    )
    inverted = true_denies_reset(text) or claims(text, r"\btrue\b[^.;]*\b(?:stays|left|leaves)\s+(?:halted|unreset)\b")
    return affirmed and not inverted


def says_recovery_is_attempted_not_promised(text: str) -> bool:
    """When a failed flash leaves an incident, recovery.auto_recover may try a
    reset into halt: a condition and an attempt, never a guarantee, and never
    "a failed flash leaves the board untouched"."""
    attempted = one_of(
        sentences(text),
        r"\bif\b|\bwhen\b|\bafter\b|\bunless\b",
        r"\bfail",
        r"\bauto_recover\b",
        r"\b(?:tries|try|attempts?|may|can)\b",
        r"\breset\b",
        r"\bhalt",
    )
    promised = claims(
        text,
        r"\b(?:always|guarantee[sd]?|every failed|each failed|ensures?)\b[^.;]*\b(?:recover\w*|reset\w*)\b"
        r"|\b(?:recover\w*|reset)\b[^.;]*\b(?:always|guaranteed)\b",
    )
    untouched = claims(text, r"\bfail\w*\b[^.;]*\b(?:untouched|unchanged|as it was|not reset)\b")
    return attempted and not promised and not untouched


def says_timeout_bound(text: str) -> bool:
    """timeout_s, in seconds, 60 by default, caps each debugger command and
    running out answers `timeout`; never a bound on the whole call, never a
    unit other than seconds, and 60 is no ceiling."""
    per_command = one_of(clauses(text), r"\btimeout_s\b", r"\b(?:each|every|per)\b[^.;]*\bcommand\b", TIMEOUT_RESULT)
    unit = one_of(clauses(text), r"\btimeout_s\b", r"\bseconds?\b")
    default = one_of(clauses(text), r"\btimeout_s\b", rf"\b{DEFAULT_DEBUGGER_TIMEOUT_S}\b", r"\bdefault|\bunless set\b|\bif unset\b|\bwhen unset\b")
    wrong_unit = claims(text, r"\btimeout_s\b[^.;]*\b(?:ms|milliseconds?|minutes?)\b|\b\d+\s*(?:ms|milliseconds?)\b")
    whole_call = claims(
        text,
        r"\b(?:whole|entire|total|overall)\s+(?:call|flash|operation|run)\b"
        r"|\b(?:the|each|every)\s+call\b[^.;]*\btimeout_s\b|\btimeout_s\b[^.;]*\b(?:the|each|every)\s+call\b",
    )
    ceiling = claims(text, rf"\b(?:at most|up to|maximum(?: of)?|no more than)\s+{DEFAULT_DEBUGGER_TIMEOUT_S}\b")
    return per_command and unit and default and not wrong_unit and not whole_call and not ceiling


def says_one_image_input(text: str) -> bool:
    """image_path or artifact_id, exactly one; never both together."""
    either = one_of(clauses(text), r"\bimage_path\b", r"\bartifact_id\b", r"\bnot both\b|\bexactly one\b|\bone of\b|\beither\b|\binstead\b|\bnot with\b")
    both = claims(text, r"\bboth\b[^.;]*\b(?:required|needed|must)\b|\b(?:requires?|needs?)\s+both\b")
    return either and not both


def says_every_call_flashes_again(text: str) -> bool:
    """A repeated call programs the board again; never "a repeated call is skipped"."""
    again = one_of(clauses(text), r"\b(?:every|each|a repeated|another)\s+(?:call|flash)\b", r"\b(?:again|anew|rewrites?|reprograms?|re-?flash\w*)\b")
    skipped = claims(text, r"\b(?:skips?|skipped|no-op|unchanged image|only if (?:it )?changed|already (?:flashed|there))\b")
    return again and not skipped


def says_bin_needs_flash_address(text: str) -> bool:
    """A .bin needs flash_address on every backend; never only on some of them (#680).

    A .bin carries no load address, so each backend is handed the field: OpenOCD
    as the `program` offset, pyOCD as `--base-address`, STM32CubeProgrammer after
    the file. A sentence that names one or two backends as the ones that need it
    tells the reader the third finds the address somewhere else."""
    everywhere = one_of(clauses(text), r"\.bin\b", r"\bflash_address\b", r"\b(?:every|each|any|all|whichever)\s+(?:backends?|debuggers?)\b")
    narrowed = any(
        re.search(r"\.bin\b", part) and re.search(r"\bflash_address\b", part) and re.search(r"\b(?:OpenOCD|pyOCD|STM32CubeProgrammer)\b", part, re.IGNORECASE)
        for part in clauses(text)
    )
    exempt = claims(text, r"\b(?:OpenOCD|pyOCD|STM32CubeProgrammer)\b[^.;]*\b(?:ignores?|takes? the load address|needs? no|does not need)\b")
    return everywhere and not narrowed and not exempt


def says_pyocd_does_not_verify(text: str) -> bool:
    """pyOCD runs no verify step; never OpenOCD or STM32CubeProgrammer named as the one that does not."""
    unverified = any(
        re.search(r"\bpyOCD\b", part, re.IGNORECASE) and (denied(r"\bverif\w*", part) or re.search(r"\bverify false\b|\bunverified\b", part, re.IGNORECASE))
        for part in clauses(text)
    )
    wrong = claims(
        text,
        r"\b(?:OpenOCD|STM32CubeProgrammer)\s+(?:does\s+not|doesn't|never|cannot)\s+verif"
        r"|\bpyOCD\s+(?:also\s+)?verifies\b|\b(?:every|all)\s+backends?\s+verif",
    )
    return unverified and not wrong


def says_capture_failure_keeps_the_image(text: str) -> bool:
    """A read that fails after a good flash makes the call ok false with the image written; never "nothing written"."""
    kept = one_of(
        clauses(text),
        r"\b(?:read|capture)\b",
        r"\bfail",
        r"\bafter\b",
        r"\bflash",
        rf"{OK_WORD}[^.;]*\bfalse\b",
        r"\b(?:written|flashed|kept|stays|on the board|committed)\b",
    )
    undone = claims(text, r"\b(?:read|capture)\b[^.;]*\bfail\w*[^.;]*\b(?:nothing (?:is )?written|not (?:written|flashed)|rolled back|undone|erased)\b")
    return kept and not undone


def names_no_backend_restriction(text: str) -> bool:
    """Every configured backend flashes: no "OpenOCD only", no "only through pyOCD"."""
    return not claims(text, rf"\b{BACKENDS}(?:\s+backend)?\s+only\b|\bonly\s+(?:on|with|through|via|for)?\s*(?:the\s+)?{BACKENDS}\b")


def says_what_it_writes_and_through_what(text: str) -> bool:
    """It writes firmware to the board or target, through the debugger, programmer or probe."""
    return (
        claims(text, r"\b(?:flash(?:es)?|programs?|writes?)\b")
        and claims(text, r"\bboard\b|\btarget\b")
        and claims(text, r"\bdebugger\b|\bprogrammer\b|\bprobe\b")
        and names_no_backend_restriction(text)
    )


def says_content_addressed(text: str) -> bool:
    """The id is the sha256 of the bytes plus the lowercased extension, so the
    same bytes and the same extension give the same id; never random, and never
    the same id for the same bytes whatever the extension."""
    digest = one_of(clauses(text), r"\bsha-?256\b", r"\bextension\b|\bsuffix\b")
    lowered = one_of(clauses(text), r"\blower", r"\bextension\b|\bsuffix\b")
    stable = one_of(
        clauses(text),
        r"\bsame\b|\bidentical\b|\bequal\b",
        r"\bbytes\b[^.;]*\band\b[^.;]*\b(?:extension|suffix)\b|\b(?:extension|suffix)\b[^.;]*\band\b[^.;]*\bbytes\b",
        r"\bid\b|\bartifact_id\b",
    )
    either = claims(
        text,
        r"\bbytes\s+or\s+(?:the\s+)?(?:same\s+)?(?:extension|suffix)\b|\b(?:extension|suffix)\s+or\s+(?:the\s+)?(?:same\s+)?bytes\b"
        r"|\bsame bytes,\s+same id\b|\bwhatever (?:the )?(?:extension|name)\b",
    )
    random = claims(text, r"\brandom\b|\buuid\b|\bnew id (?:each|every)\b|\bunique (?:per|for each) upload\b")
    return digest and lowered and stable and not either and not random


def says_whitespace_ignored(text: str) -> bool:
    ignored = one_of(clauses(text), r"\bwhitespace\b|\bspaces?\b|\bline breaks?\b|\bnewlines?\b", r"\bignored\b|\bremoved\b|\bskipped\b|\bstripped\b|\ballowed\b")
    refused = claims(text, r"\b(?:whitespace|spaces?|line breaks?|newlines?)\b[^.;]*\b(?:invalid|not allowed|refused|rejected|forbidden)\b")
    return ignored and not refused


def says_size_limit(text: str) -> bool:
    """The decoded image, at most max_upload_size_mb MiB (64 by default), and
    over it artifact_too_large; never the encoded size, never another unit, never
    "below it", and 64 is no fixed ceiling."""
    limit = one_of(
        clauses(text),
        r"\bmax_upload_size_mb\b",
        r"\bMiB\b|\bmebibytes?\b|\b1024\s*[x*]\s*1024\b",
        r"\bdecod",
        r"\bartifact_too_large\b",
        r"\bat most\b|\bover\b|\babove\b|\bexceed|\bmore than\b|\belse\b|\bbeyond\b|\blarger\b",
    )
    default = one_of(clauses(text), r"\bmax_upload_size_mb\b", rf"\b{DEFAULT_MAX_UPLOAD_SIZE_MB}\b", r"\bdefault")
    inverted = claims(text, r"\b(?:below|under|less than|smaller than|beneath)\b[^.;]*\bartifact_too_large\b|\bartifact_too_large\b[^.;]*\b(?:below|under|less than|smaller than)\b")
    encoded = any(
        re.search(r"\bmax_upload_size_mb\b", part) and re.search(r"\bencoded\b|\bbase64 (?:text|string|length|size)\b|\bbefore decoding\b", part, re.IGNORECASE)
        for part in clauses(text)
    )
    wrong_unit = claims(text, rf"\bmax_upload_size_mb\s+(?:bytes|KiB|KB|kilobytes?)\b|\b{DEFAULT_MAX_UPLOAD_SIZE_MB}\s*(?:bytes|KiB|KB|kilobytes?)\b")
    ceiling = claims(text, rf"\b(?:at most|up to|maximum(?: of)?|no more than)\s+{DEFAULT_MAX_UPLOAD_SIZE_MB}\b")
    return limit and default and not inverted and not encoded and not wrong_unit and not ceiling


def says_bare_filename(text: str) -> bool:
    """A bare file name, no path, whose extension sets the id's suffix. A negation
    counts only when it reaches the path: "a path, it doesn't matter which" allows one."""
    path = r"\bpaths?\b|\bdirector\w*|\bseparators?\b|/"
    bare = any(re.search(path, part, re.IGNORECASE) and (re.search(r"\bbare\b", part, re.IGNORECASE) or denied(path, part)) for part in clauses(text))
    suffix = one_of(clauses(text), r"\bextension\b|\bsuffix\b", r"\bartifact_id\b|\bid\b")
    return bare and suffix


def says_touches_no_board(text: str) -> bool:
    """No board, probe or debugger is involved; never "needs a debugger"."""
    untouched = any(denied(r"\bboard\b|\bdebugger\b|\bhardware\b|\bprobe\b|\btarget\b", part) for part in clauses(text))
    needs = claims(text, r"\b(?:needs?|requires?)\s+(?:a|an|the)\s+(?:bound\s+|configured\s+|connected\s+)?(?:debugger|probe|board|target)\b")
    return untouched and not needs


def says_stored_in_upload_directory(text: str) -> bool:
    """The image is stored in artifacts.upload_directory; never "kept in memory" or "not stored"."""
    stored = one_of(clauses(text), r"\bupload_directory\b", r"\b(?:stores?|stored|saves?|saved|keeps?|kept|writes?|written|copies|copied)\b")
    elsewhere = claims(text, r"\b(?:memory only|in memory|not (?:stored|saved|kept)|nothing is (?:stored|saved|kept)|temporar)")
    return stored and not elsewhere


def says_outer_ok_even_for_a_failed_report(text: str) -> bool:
    """get_last_report answers ok true even when the stored report failed; the stored verdict is report.ok."""
    true_anyway = one_of(clauses(text), OK_WORD, r"\btrue\b", r"\bfail")
    inverted = one_of(clauses(text), rf"{OK_WORD}[^.;]*\bfalse\b", r"\bfail") or claims(text, rf"{OK_WORD}[^.;]*\b(?:never|not|isn't)\b[^.;]*\btrue\b")
    return true_anyway and claims(text, r"\breport\.ok\b") and not inverted


def says_how_to_judge_the_stored_verdict(text: str) -> bool:
    """report.ok is read together with report.audit_ok and report.cleanup_required
    (or report.quarantined): a report can say ok and still have failed."""
    together = one_of(clauses(text), r"\breport\.ok\b", r"\breport\.audit_ok\b", r"\breport\.(?:cleanup_required|quarantined)\b")
    alone = claims(text, r"\breport\.ok\s+alone\b|\bonly\s+report\.ok\b|\breport\.audit_ok\b[^.;]*\b(?:does not|doesn't|do not|don't|never)\s+matter")
    return together and not alone


def says_newest_report_may_be_the_recovery(text: str) -> bool:
    """After a failed call the newest report can be the recovery's reset_target or probe_target."""
    may = any(
        re.search(r"\brecover", sentence, re.IGNORECASE) and re.search(r"\breset_target\b|\bprobe_target\b", sentence) and not re.search(NEGATION, sentence, re.IGNORECASE)
        for sentence in sentences(text)
    )
    own = claims(text, r"\balways\b[^.;]*\b(?:your|the caller's|the last call you made|own)\b")
    return may and claims(text, r"\breport\.tool\b") and not own


def says_reading_changes_nothing(text: str) -> bool:
    """Reading the report consumes nothing, so a repeated read answers the same; never "reading clears it"."""
    unchanged = one_of(
        clauses(text),
        r"\bread",
        r"\bchanges nothing\b|\bconsumes nothing\b|\bnothing (?:is )?(?:changed|consumed|cleared)\b|\b(?:does not|doesn't|never)\s+(?:change|consume|clear)\b|\brepeat",
    )
    consumed = any(re.search(r"\bread", part, re.IGNORECASE) and stated(r"\b(?:clears?|consumes?|removes?|deletes?|resets?)\b", part) for part in clauses(text))
    return unchanged and not consumed


CLEARED_BY_SUCCESS = (
    r"\b(?:clear|clears|cleared|reset|resets|erase|erases|erased|forgotten|replaced|overwritten)\b[^.;:,]*\bsuccess"
    r"|\bsuccess\w*\b[^.;:,]*\b(?:clears?|cleared|resets?|erases?|erased|replaces?|overwrites?)\b"
)


def says_record_outlives_successes(text: str) -> bool:
    """The failure record stays until a newer failure: a success does not clear it.

    Clearing is checked within one phrase ("a success clears it", "cleared by a
    success"), and a negated phrase is the claim itself, not its inversion: the
    negation within it ("a success does not clear it") or reaching it from
    before ("it is not cleared by a success")."""
    stays = one_of(clauses(text), r"\bsuccess|\bsucceed", r"\bstays?\b|\bremains?\b|\bkept\b|\bkeeps?\b|\bpersists?\b|\bsurvives?\b|\buntil\b")
    cleared = any(
        not re.search(NEGATION, match.group(0), re.IGNORECASE) and not denies(match) for match in re.finditer(CLEARED_BY_SUCCESS, text, re.IGNORECASE)
    )
    return stays and not cleared


def says_some_refusals_record_none(text: str) -> bool:
    """The allow_flash refusal (and a schema refusal) leave no record; never "every call is recorded"."""
    none = one_of(clauses(text), r"\brefus|\bpermission_denied\b|\binvalid_argument\b", r"\ballow_flash\b", r"\bno\b|\bnone\b|\bnot\b|\bnothing\b|\bwithout\b")
    every = claims(text, r"\b(?:every|each|all)\b[^.;]*\b(?:calls?|refusals?|failures?|errors?)\b[^.;]*\b(?:recorded|writes?|written|records?)\b")
    return none and not every


def says_when_nothing_is_stored(text: str) -> bool:
    """report_not_found means nothing is stored yet; never a damaged or unreadable record."""
    absent = one_of(clauses(text), r"\breport_not_found\b", r"\bbefore\b|\bno\b|\bnot yet\b|\bnone\b|\bnothing\b|\byet\b")
    misread = any(
        re.search(r"\breport_not_found\b", part) and re.search(r"\b(?:damaged|corrupt\w*|unreadable|malformed|invalid|broken)\b", part, re.IGNORECASE)
        for part in clauses(text)
    )
    return absent and not misread


UNREADABLE = r"\breading it failed\b|\bcannot be read\b|\bcould not be read\b|\bunreadable\b"
DAMAGED = r"\b(?:damaged|malformed|corrupt\w*)\b"


def says_damaged_state_answers_report_state_damaged(clause: str) -> bool:
    """A damaged state and `report_state_damaged` in one clause, in either order, that no
    negation reaches, between them or before them: "a damaged one answers
    `report_state_damaged`", never "a damaged one never answers `report_state_damaged`" and
    never "No damaged report state answers `report_state_damaged`"."""
    unnegated = rf"(?:(?!{NEGATION})[^.;])*?"
    return stated(rf"{DAMAGED}{unnegated}\breport_state_damaged\b|\breport_state_damaged\b{unnegated}{DAMAGED}", clause)


def says_unreadable_state_apart(meaning: str) -> bool:
    """The catalogue's `report_unreadable`: a report state that exists and could
    not be read (an OSError or ValueError on the read), apart from one that reads
    and is damaged, which raises `report_state_damaged` (it answered
    `config_invalid` until #689), and from one that is not there (`report_not_found`)."""
    unreadable = one_of(clauses(meaning), r"\bexists?\b", UNREADABLE)
    damaged = any(says_damaged_state_answers_report_state_damaged(part) for part in clauses(meaning))
    merged = any(re.search(r"\breport_not_found\b|\breport_state_damaged\b", part) and re.search(UNREADABLE, part, re.IGNORECASE) for part in clauses(meaning))
    return unreadable and damaged and not merged


def says_source_may_be_the_recovery_reset(text: str) -> bool:
    return any(
        re.search(r"\bsource_tool\b", sentence)
        and re.search(r"\breset_target\b", sentence)
        and re.search(r"\bflash_firmware\b|\brecover", sentence, re.IGNORECASE)
        and not re.search(NEGATION, sentence, re.IGNORECASE)
        for sentence in sentences(text)
    )


def says_until_limits(text: str) -> bool:
    """Up to 8 texts, each at most 256 characters; characters, not bytes."""
    limits = one_of(clauses(text), rf"\b{UNTIL_MAX_ENTRIES}\b", rf"\b{UNTIL_MAX_CHARACTERS}\b[^.;]*\bcharacters?\b|\bcharacters?\b[^.;]*\b{UNTIL_MAX_CHARACTERS}\b")
    return limits and not claims(text, rf"\b{UNTIL_MAX_CHARACTERS}\s+bytes\b")


def says_max_bytes_default(text: str) -> bool:
    """Without max_bytes the capture returns up to the port's max_buffer_bytes."""
    return one_of(clauses(text), r"\bmax_buffer_bytes\b", r"\bdefault|\bunless set\b|\bif unset\b|\bwhen unset\b")


def says_wait_bounds(text: str) -> bool:
    """Seconds after the flash ends, 10 by default and 60 at most; never milliseconds."""
    bounded = one_of(
        sentences(text),
        r"\bseconds?\b",
        rf"\b{CAPTURE_DEFAULT_WAIT_S}\b[^.;]*\bdefault|\bdefault[^.;]*\b{CAPTURE_DEFAULT_WAIT_S}\b",
        rf"\b{CAPTURE_MAX_WAIT_S}\b[^.;]*\bat most\b|\bat most\b[^.;]*\b{CAPTURE_MAX_WAIT_S}\b|\bup to\s+{CAPTURE_MAX_WAIT_S}\b",
    )
    return bounded and not claims(text, r"\b(?:ms|milliseconds?)\b")


def test_the_four_tools_are_listed_with_a_description_within_the_budget(listed: dict[str, dict]) -> None:
    for name in TOOLS:
        assert name in listed, sorted(listed)
        description = listed[name].get("description")
        assert isinstance(description, str) and description.strip(), name
        assert len(description) <= DESCRIPTION_LIMIT, (name, len(description))


@pytest.mark.parametrize("name", TOOLS)
def test_every_input_property_describes_itself_at_any_depth(listed: dict[str, dict], name: str) -> None:
    """Top-level properties, the capture object's own and anything inside a
    oneOf, anyOf, allOf or array, each within the property budget."""
    found = list(described_schemas(listed[name]["inputSchema"]))
    undescribed = sorted(".".join(path) for path, child in found if not str(child.get("description") or "").strip())
    assert not undescribed, f"{name}: input properties without a description: {undescribed}"
    oversized = sorted(".".join(path) for path, child in found if len(str(child.get("description") or "")) > PROPERTY_DESCRIPTION_LIMIT)
    assert not oversized, f"{name}: property descriptions over {PROPERTY_DESCRIPTION_LIMIT} characters: {oversized}"


def test_the_walk_reaches_the_nested_capture_inputs(listed: dict[str, dict]) -> None:
    paths = {path for path, _ in described_schemas(listed[FLASH]["inputSchema"])}
    assert {("capture", "port_id"), ("capture", "until"), ("capture", "wait_timeout_s"), ("capture", "max_bytes")} <= paths, sorted(paths)


def test_the_report_tools_take_no_input(listed: dict[str, dict]) -> None:
    for name in REPORT_TOOLS:
        assert listed[name]["inputSchema"]["properties"] == {}, name


@pytest.mark.parametrize("name", TOOLS)
def test_the_definitions_use_the_public_words_for_the_hardware(listed: dict[str, dict], name: str) -> None:
    """The unit is the in-circuit debugger or programmer and the target is a
    board. Backends go by their tool names (OpenOCD, pyOCD,
    STM32CubeProgrammer), never by the probe's."""
    text = every_description(listed[name])
    assert not claims(text, r"\bST-?Link\b"), text
    assert not re.search(r"\bSTM32\b(?!CubeProgrammer)", text), text


def test_flash_firmware_says_what_it_writes_to_and_through_what(listed: dict[str, dict]) -> None:
    """Firmware onto the board through the in-circuit debugger or programmer,
    with no backend restriction: every configured backend flashes."""
    description = str(listed[FLASH]["description"])
    assert says_what_it_writes_and_through_what(description), description
    assert names_no_backend_restriction(every_description(listed[FLASH])), every_description(listed[FLASH])


def test_flash_firmware_says_every_call_flashes_again(listed: dict[str, dict]) -> None:
    text = text_of(listed, FLASH)
    assert says_every_call_flashes_again(text), text


def test_flash_firmware_names_allow_flash_and_its_refusal(listed: dict[str, dict]) -> None:
    text = text_of(listed, FLASH)
    assert needs_with_refusal(text, "allow_flash"), text


def test_flash_firmware_says_a_debug_session_on_the_probe_makes_it_busy(listed: dict[str, dict]) -> None:
    text = text_of(listed, FLASH)
    assert says_debug_session_makes_flash_busy(text), text


def test_flash_firmware_names_another_owner_holding_the_probe(listed: dict[str, dict]) -> None:
    text = text_of(listed, FLASH)
    assert not names(text, "device_busy"), text


def test_reset_after_flash_names_its_default_its_effect_and_its_permission(listed: dict[str, dict]) -> None:
    schema = listed[FLASH]["inputSchema"]["properties"]["reset_after_flash"]
    described = property_text(listed[FLASH], "reset_after_flash")
    assert schema.get("default") is False, schema
    assert says_reset_default_is_false(described), described
    assert says_true_resets(described), described
    assert needs_with_refusal(described, "allow_reset"), described


def test_flash_firmware_says_recovery_is_attempted_not_promised(listed: dict[str, dict]) -> None:
    text = text_of(listed, FLASH)
    assert says_recovery_is_attempted_not_promised(text), text


def test_flash_firmware_names_what_bounds_each_debugger_command(listed: dict[str, dict]) -> None:
    text = text_of(listed, FLASH)
    assert says_timeout_bound(text), text


def test_flash_firmware_takes_one_image_from_a_permitted_place(listed: dict[str, dict]) -> None:
    """image_path: the workspace, the allowed roots (`build` when the key is
    unset) and the allowed extensions; artifact_id: what artifact_upload
    returned, usable only while uploads are allowed. Exactly one of the two."""
    text = text_of(listed, FLASH)
    image_path = property_text(listed[FLASH], "image_path")
    artifact_id = property_text(listed[FLASH], "artifact_id")
    assert says_one_image_input(text), text
    assert not names(image_path, "allowed_roots", *DEFAULT_EXTENSIONS), image_path
    assert one_of(clauses(image_path), rf"\b{DEFAULT_ALLOWED_ROOT}\b", r"\bunset\b|\bomitted\b|\bdefault\b|\bnot set\b|\bleft out\b"), image_path
    assert claims(image_path, r"\bworkspace\b"), image_path
    assert not names(artifact_id, "artifact_upload", "allow_upload"), artifact_id
    assert needs_with_refusal(artifact_id, "allow_upload"), artifact_id


def test_image_path_says_which_backends_need_flash_address_and_which_does_not_verify(listed: dict[str, dict]) -> None:
    described = property_text(listed[FLASH], "image_path")
    assert says_bin_needs_flash_address(described), described
    assert says_pyocd_does_not_verify(described), described


def test_flash_firmware_names_the_image_refusals(listed: dict[str, dict]) -> None:
    text = text_of(listed, FLASH)
    missing = names(text, "artifact_validation_failed", "artifact_not_found", "artifact_too_large", "max_upload_size_mb")
    assert not missing, (missing, text)
    assert one_of(clauses(text), r"\bartifact_too_large\b", r"\bmax_upload_size_mb\b"), text


def test_capture_says_a_read_that_fails_after_a_good_flash_leaves_the_image_written(listed: dict[str, dict]) -> None:
    described = property_text(listed[FLASH], "capture")
    assert says_capture_failure_keeps_the_image(described), described


def test_the_capture_inputs_name_their_limits_and_defaults(listed: dict[str, dict]) -> None:
    tool = listed[FLASH]
    until = nested_text(tool, "capture", "until")
    max_bytes = nested_text(tool, "capture", "max_bytes")
    wait = nested_text(tool, "capture", "wait_timeout_s")
    assert says_until_limits(until), until
    assert says_max_bytes_default(max_bytes), max_bytes
    assert says_wait_bounds(wait), wait


def test_artifact_upload_says_what_it_stores_where_and_who_uses_the_id(listed: dict[str, dict]) -> None:
    description = str(listed[UPLOAD]["description"])
    missing = names(description, "artifact_id", FLASH, "debug_start_session", "upload_directory")
    assert not missing, (missing, description)
    assert one_of(sentences(description), r"\bartifact_id\b", r"\bflash_firmware\b", r"\bdebug_start_session\b"), description
    assert says_stored_in_upload_directory(description), description
    assert says_touches_no_board(text_of(listed, UPLOAD)), text_of(listed, UPLOAD)


def test_artifact_upload_names_its_permission_and_its_refusals(listed: dict[str, dict]) -> None:
    text = text_of(listed, UPLOAD)
    assert needs_with_refusal(text, "allow_upload"), text
    missing = names(text, "artifact_validation_failed", "artifact_too_large", "artifact_not_found", "invalid_argument", "unsafe_configured_path")
    assert not missing, (missing, text)


def test_artifact_upload_names_its_two_inputs(listed: dict[str, dict]) -> None:
    """image_path, or filename together with data_base64."""
    text = text_of(listed, UPLOAD)
    assert one_of(sentences(text), r"\bimage_path\b", r"\bfilename\b", r"\bdata_base64\b", r"\bor\b"), text
    assert one_of(clauses(text), r"\bfilename\b", r"\bdata_base64\b", r"\bwith\b|\band\b|\btogether\b|\bneeded\b|\brequired\b"), text


def test_the_artifact_id_is_described_as_content_addressed(listed: dict[str, dict]) -> None:
    text = text_of(listed, UPLOAD)
    assert says_content_addressed(text), text


def test_data_base64_names_its_encoding_whitespace_and_size_limit(listed: dict[str, dict]) -> None:
    described = property_text(listed[UPLOAD], "data_base64")
    assert one_of(clauses(described), r"\bbase64\b", r"\bpadd"), described
    assert says_whitespace_ignored(described), described
    assert says_size_limit(described), described


def test_filename_is_a_bare_name_whose_extension_ends_the_id(listed: dict[str, dict]) -> None:
    described = property_text(listed[UPLOAD], "filename")
    assert says_bare_filename(described), described
    assert claims(described, r"\bdata_base64\b"), described


def test_the_upload_image_path_names_roots_and_extensions(listed: dict[str, dict]) -> None:
    described = property_text(listed[UPLOAD], "image_path")
    assert not names(described, "allowed_roots", *DEFAULT_EXTENSIONS), described
    assert one_of(clauses(described), rf"\b{DEFAULT_ALLOWED_ROOT}\b", r"\bunset\b|\bomitted\b|\bdefault\b|\bnot set\b|\bleft out\b"), described
    assert claims(described, r"\bworkspace\b"), described


def test_get_last_report_says_what_it_returns_and_how_to_read_it(listed: dict[str, dict]) -> None:
    text = text_of(listed, LAST_REPORT)
    missing = names(text, "report_not_found", CLASSIFY)
    assert not missing, (missing, text)
    assert says_outer_ok_even_for_a_failed_report(text), text
    assert says_how_to_judge_the_stored_verdict(text), text
    assert says_reading_changes_nothing(text), text
    assert one_of(sentences(text), r"\bclassify_last_error\b", r"\bfail"), text


def test_get_last_report_says_the_newest_report_can_be_the_recovery(listed: dict[str, dict]) -> None:
    text = text_of(listed, LAST_REPORT)
    assert says_newest_report_may_be_the_recovery(text), text


@pytest.mark.parametrize(("name", "other"), [(LAST_REPORT, CLASSIFY), (CLASSIFY, LAST_REPORT)])
def test_each_report_tool_names_the_other_in_its_first_two_sentences(listed: dict[str, dict], name: str, other: str) -> None:
    """Both read the same stored reports, so each says up front what it gives
    that the other does not, and names the other."""
    opening = " ".join(sentences(listed[name]["description"])[:2])
    assert names(opening, other) == [], opening


@pytest.mark.parametrize("name", REPORT_TOOLS)
def test_the_report_tools_say_report_not_found_means_nothing_is_stored(listed: dict[str, dict], name: str) -> None:
    text = text_of(listed, name)
    assert says_when_nothing_is_stored(text), text


def test_the_catalogue_tells_an_unreadable_report_state_from_a_damaged_or_missing_one(tmp_path: Path) -> None:
    """Which read failure is which left the report tools' descriptions for the
    catalogue, where a refusal's remediation sends its caller. Resolved through
    the resource a host reads: `report_unreadable` is a state that exists and
    could not be read, and one that reads and is damaged answers `report_state_damaged`."""
    service = new_service(tmp_path / "catalogue")
    try:
        entry = json.loads(read_text(service, ERROR_URI_PREFIX + "report_unreadable"))
    finally:
        close(service)
    assert says_unreadable_state_apart(entry["meaning"]), entry["meaning"]


@pytest.mark.parametrize(
    ("pattern", "replacement"),
    [
        (r"\banswers `report_state_damaged`", "never answers `report_state_damaged`"),
        (r"\banswers `report_state_damaged`", "cannot answer `report_state_damaged`"),
        (r"\bis damaged answers `report_state_damaged` instead", "is damaged answers this as well"),
        (r"A report state that reads and is damaged answers `report_state_damaged` instead\.", "No damaged report state answers `report_state_damaged`."),
    ],
    ids=["negated", "cannot", "merged", "subject"],
)
def test_the_unreadable_check_refuses_a_served_entry_that_inverts_the_damaged_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pattern: str, replacement: str) -> None:
    """A malformed report state raises `report_state_damaged` (#689); only an
    OSError or ValueError on the read is `report_unreadable`.
    The same entry, with that mapping negated or folded into this one, has to fail
    the check when it is read the way a host reads it."""
    planted, count = re.subn(pattern, replacement, ERROR_CATALOGUE["report_unreadable"].meaning)
    assert count == 1, ERROR_CATALOGUE["report_unreadable"].meaning
    monkeypatch.setitem(ERROR_CATALOGUE, "report_unreadable", dataclasses.replace(ERROR_CATALOGUE["report_unreadable"], meaning=planted))
    service = new_service(tmp_path / "catalogue")
    try:
        entry = json.loads(read_text(service, ERROR_URI_PREFIX + "report_unreadable"))
    finally:
        close(service)
    assert entry["meaning"] == planted
    assert not says_unreadable_state_apart(entry["meaning"]), entry["meaning"]


def test_classify_last_error_names_its_result_fields(listed: dict[str, dict]) -> None:
    text = text_of(listed, CLASSIFY)
    missing = names(text, "error_type", "likely_causes", "source_tool", "report_path", "log_path", "report_not_found")
    assert not missing, (missing, text)
    assert says_when_nothing_is_stored(text), text


def test_classify_last_error_says_the_record_outlives_successes(listed: dict[str, dict]) -> None:
    text = text_of(listed, CLASSIFY)
    assert says_record_outlives_successes(text), text


def test_classify_last_error_says_the_source_can_be_the_recovery_reset(listed: dict[str, dict]) -> None:
    text = text_of(listed, CLASSIFY)
    assert says_source_may_be_the_recovery_reset(text), text


def test_classify_last_error_says_some_refusals_record_nothing(listed: dict[str, dict]) -> None:
    text = text_of(listed, CLASSIFY)
    assert says_some_refusals_record_none(text), text
    # The rule covers the interlock too, which refuses before the lease as
    # allow_flash does (#679): a reader told only of allow_flash would expect a
    # record of an allow_mass_erase refusal.
    assert one_of(clauses(text), r"\ballow_mass_erase\b", r"\bnothing\b|\bnone\b|\bno record\b"), text


@pytest.mark.parametrize("name", REPORT_TOOLS)
def test_the_report_tools_say_they_need_no_board_or_debugger(listed: dict[str, dict], name: str) -> None:
    text = text_of(listed, name)
    assert says_touches_no_board(text), text


def test_the_annotations_agree_with_what_the_definitions_describe(listed: dict[str, dict]) -> None:
    """A flash erases what was there and repeats its effect; an upload adds a
    content-addressed file; the report tools only read. The text that says so
    is checked above; these hints only have to agree with it."""
    flash_hints = listed[FLASH]["annotations"]
    assert (flash_hints["readOnlyHint"], flash_hints["destructiveHint"], flash_hints["idempotentHint"]) == (False, True, False)
    upload_hints = listed[UPLOAD]["annotations"]
    assert (upload_hints["readOnlyHint"], upload_hints["destructiveHint"], upload_hints["idempotentHint"]) == (False, False, True)
    for name in REPORT_TOOLS:
        assert listed[name]["annotations"]["readOnlyHint"] is True, name
    for name in TOOLS:
        assert listed[name]["annotations"]["openWorldHint"] is False, name


# What each relation accepts and what it rejects, so a check that passes for the
# wrong reason fails here instead.
PARAPHRASES = [
    (
        needs_with_refusal,
        ("Needs allow_flash, else permission_denied.", "Without allow_flash: permission_denied.", "permission_denied unless allow_flash is granted."),
        (
            "Needs no allow_flash; permission_denied comes from elsewhere.",
            "allow_flash is optional.",
            "allow_flash true: permission_denied.",
            "Needs allow_flash, else permission_denied; allow_flash true also answers permission_denied.",
            "allow_flash and permission_denied.",
        ),
    ),
    (
        says_debug_session_makes_flash_busy,
        ("While a debug session holds the probe it answers resource_busy until debug_stop_session.", "resource_busy: debug_stop_session first."),
        ("It stops the debug session first, then flashes; resource_busy is gone after debug_stop_session.", "resource_busy means another server."),
    ),
    (
        says_reset_default_is_false,
        ("Default false: the board is not reset.", "Default false: not reset; true doesn't verify and resets after flash."),
        ("Defaults to true, so the board is reset.", "False by default.", "Default false: not reset; true never resets either.", "Default false: the board is reset and nothing is verified."),
    ),
    (
        says_true_resets,
        (
            "Reset the board after flashing.",
            "True: the board is reset once flashed.",
            "On pyOCD, true resets the board after flash and doesn't verify it.",
            "On pyOCD, true doesn't verify and resets the board after flashing.",
        ),
        ("True never resets the board after flashing.", "True: the board is not reset after flashing.", "Resets the board."),
    ),
    (
        says_recovery_is_attempted_not_promised,
        ("If a failed flash leaves an incident, recovery.auto_recover may try a reset into halt.", "When a flash fails, recovery.auto_recover attempts a reset into halt."),
        (
            "A failed flash leaves the board untouched.",
            "A failed flash is always followed by a recovery reset into halt (recovery.auto_recover).",
            "If a flash fails, recovery.auto_recover resets the board into halt.",
            "If a failed flash leaves an incident, recovery.auto_recover may try a reset into halt; a failed flash leaves the board unchanged.",
        ),
    ),
    (
        says_timeout_bound,
        ("The debugger entry's timeout_s (seconds, default 60) caps each debugger command, else timeout.", "Each command may take timeout_s seconds (60 unless set), else timeout."),
        (
            "Bounded by timeout_s (default 60), else timeout.",
            "timeout_s (default 60 ms) caps each debugger command, else timeout.",
            "timeout_s (seconds, default 60) caps the whole call, else timeout.",
            "timeout_s (seconds, at most 60) caps each debugger command, else timeout.",
            "timeout_s defaults to 60 seconds.",
        ),
    ),
    (
        says_one_image_input,
        ("Give image_path or artifact_id, not both.", "An artifact_id, not with image_path."),
        ("Both image_path and artifact_id are required.", "Give image_path."),
    ),
    (
        says_every_call_flashes_again,
        ("Every call writes it again.", "A repeated call reprograms the board."),
        ("Every call writes it again, but an unchanged image is skipped.", "Writes the board."),
    ),
    (
        says_bin_needs_flash_address,
        ("A .bin needs flash_address on every backend.", "A .bin needs flash_address, whichever backend flashes it."),
        ("A .bin needs flash_address on pyOCD and STM32CubeProgrammer.", "A .bin needs flash_address on every backend; OpenOCD ignores it.", "A .bin needs flash_address."),
    ),
    (
        says_pyocd_does_not_verify,
        ("pyOCD does not verify (verify false).", "OpenOCD and STM32CubeProgrammer verify; pyOCD does not verify."),
        ("OpenOCD does not verify; pyOCD does not either.", "pyOCD verifies.", "Every backend verifies.", "On pyOCD the flash doesn't halt the core and verifies the image."),
    ),
    (
        says_capture_failure_keeps_the_image,
        ("A read failing after a good flash is ok false, image written.", "If the capture fails after the flash, ok is false and the firmware stays on the board."),
        ("A read failing after a good flash is ok false, nothing written.", "A read failing after a good flash is ok true.", "A failed read is ok false."),
    ),
    (
        names_no_backend_restriction,
        ("Write firmware through the configured backend: OpenOCD, pyOCD or STM32CubeProgrammer.",),
        ("OpenOCD backend only; others answer not_supported.", "Flashes only through pyOCD."),
    ),
    (
        says_what_it_writes_and_through_what,
        ("Write firmware to the board via the in-circuit debugger or programmer.", "Programs the target through the probe."),
        ("Write firmware to the board via the in-circuit debugger or programmer, OpenOCD only.", "Write firmware via the debugger."),
    ),
    (
        says_content_addressed,
        ("The id is the sha256 of the bytes plus the lowercased extension: same bytes and extension, same id.",),
        (
            "The id is a random uuid; the sha256 plus lowercased extension is in the result and the same bytes and extension give the same id.",
            "The id is the sha256 of the bytes.",
            "The id is the sha256 of the bytes plus the lowercased extension: same bytes or extension, same id.",
            "The id is the sha256 of the bytes plus the lowercased extension: same bytes, same id.",
            "The id is the sha256 of the bytes plus the extension: same bytes and extension, same id.",
        ),
    ),
    (says_whitespace_ignored, ("Padded base64; whitespace is ignored.",), ("Padded base64; whitespace is rejected.", "Padded base64.")),
    (
        says_size_limit,
        ("Decoded, at most artifacts.max_upload_size_mb MiB (default 64), else artifact_too_large.", "Over max_upload_size_mb MiB once decoded (64 by default): artifact_too_large."),
        (
            "At most 64 MiB once decoded (max_upload_size_mb, default 64), else artifact_too_large.",
            "max_upload_size_mb applies, default 64.",
            "Decoded, at most max_upload_size_mb bytes (default 64), else artifact_too_large.",
            "Encoded, at most max_upload_size_mb MiB (default 64), else artifact_too_large.",
            "Decoded, max_upload_size_mb MiB (default 64); below it artifact_too_large.",
            "max_upload_size_mb defaults to 64 bytes, below it artifact_too_large.",
        ),
    ),
    (says_bare_filename, ("Bare name, no path; its extension ends the artifact_id.",), ("A path to the file; its extension ends the artifact_id.", "Bare name, no path.", "A file name or a path, it doesn't matter which; its extension ends the artifact_id.")),
    (says_touches_no_board, ("Reads files only, no board or debugger needed.",), ("Needs a configured debugger.", "Reads the report files.", "It flashes the board and doesn't wait.")),
    (
        says_stored_in_upload_directory,
        ("Store a firmware image in artifacts.upload_directory.",),
        ("Keep a firmware image in memory only; artifacts.upload_directory is not used.", "Store a firmware image."),
    ),
    (
        says_outer_ok_even_for_a_failed_report,
        ("ok is true even when that report failed; read report.ok and report.error_type.",),
        ("ok is false when that report failed; read report.ok.", "ok is true even when that report failed.", "ok is never true when the report failed; read report.ok."),
    ),
    (
        says_how_to_judge_the_stored_verdict,
        ("Judge report.ok, report.audit_ok and report.cleanup_required.", "report.ok with report.audit_ok and report.quarantined."),
        ("Judge report.ok alone; report.audit_ok and report.cleanup_required do not matter.", "Judge report.ok."),
    ),
    (
        says_newest_report_may_be_the_recovery,
        ("After a failed call it can be the recovery's reset_target or probe_target: check report.tool.",),
        (
            "It is always the report of the last call you made.",
            "After a failed call it can be the recovery's reset_target.",
            "After a failed call it is never the recovery's reset_target or probe_target: check report.tool.",
        ),
    ),
    (
        says_reading_changes_nothing,
        ("Return the newest stored report; reading changes nothing.", "A repeated read answers the same report."),
        ("Return the newest stored report; reading clears it.", "Return the newest stored report.", "Repeated reads answer the same; reading clears the record and can't be undone."),
    ),
    (
        says_record_outlives_successes,
        (
            "It stays until a newer failure, across successes and restarts.",
            "It stays across successes, so a failed recovery reset names reset_target.",
            "It stays across successes; a success does not clear it.",
            "Successes and restarts keep it until a newer failure.",
            "It stays until a newer failure and is not cleared by a success.",
        ),
        ("A success clears it.", "It stays until the next success clears it.", "It stays, but is cleared by a success.", "Successes and restarts clear it until a newer failure."),
    ),
    (
        says_some_refusals_record_none,
        ("Some refusals (allow_flash off, bad arguments) record none.", "Some refusals, such as allow_flash off, record nothing."),
        ("Every refusal is recorded, allow_flash off included.", "Some refusals record none.", "All refusals (allow_flash off, bad arguments) are recorded."),
    ),
    (
        says_when_nothing_is_stored,
        ("No failure yet: report_not_found.", "None yet: report_not_found; unreadable: report_unreadable."),
        ("report_not_found means the report file is damaged.", "None yet or unreadable: report_not_found."),
    ),
    (
        says_unreadable_state_apart,
        (
            "This project's report state exists and reading it failed. A report state that reads and is damaged answers `report_state_damaged` instead.",
            "The state exists but cannot be read; a malformed one is report_state_damaged.",
        ),
        (
            "This project's report state exists and reading it failed.",
            "The report state exists and reading it failed; a damaged one answers this too.",
            "The state exists and reading it failed, or it reads and is damaged: report_state_damaged.",
            "Nothing is stored yet: report_not_found. The state exists and reading it failed, or is damaged: report_not_found or report_state_damaged.",
            "This project's report state exists and reading it failed. A report state that reads and is damaged never answers `report_state_damaged`.",
            "The state exists but cannot be read; a malformed one does not answer report_state_damaged.",
            "The state exists but cannot be read; a damaged state cannot answer `report_state_damaged`.",
            "The state exists but cannot be read; a malformed one can't answer report_state_damaged.",
            "The state exists but cannot be read; report_state_damaged is never a damaged one.",
            "This project's report state exists and reading it failed. No damaged report state answers `report_state_damaged`.",
        ),
    ),
    (
        says_source_may_be_the_recovery_reset,
        ("A failed flash_firmware whose recovery reset failed names reset_target as source_tool.",),
        ("source_tool names the tool that failed.", "source_tool is never reset_target for a failed flash_firmware."),
    ),
    (
        says_until_limits,
        ("Text to wait for, or up to 8 texts of at most 256 characters each.",),
        ("Text to wait for, or up to 8 texts of at most 256 bytes each.", "Text to wait for, or up to 8 texts."),
    ),
    (says_max_bytes_default, ("Most bytes to return; the port's max_buffer_bytes by default.",), ("Most bytes to return.", "At most max_buffer_bytes.")),
    (
        says_wait_bounds,
        ("Seconds to wait once the flash ends; 10 by default, 60 at most.",),
        ("Milliseconds to wait once the flash ends; 10 by default, 60 at most.", "Seconds to wait once the flash ends.", "Seconds to wait, 60 by default."),
    ),
]


@pytest.mark.parametrize(("check", "accepted", "rejected"), PARAPHRASES, ids=[check.__name__ for check, _, _ in PARAPHRASES])
def test_each_relation_accepts_its_paraphrases_and_rejects_their_inversions(check, accepted: tuple[str, ...], rejected: tuple[str, ...]) -> None:
    arguments = ("allow_flash",) if check is needs_with_refusal else ()
    for text in accepted:
        assert check(text, *arguments), text
    for text in rejected:
        assert not check(text, *arguments), text


# One meaning flipped in the listed definition: the whole tool, every
# description in it, with the scope the check reads. None is the description
# and the top-level property descriptions; a tuple is one (nested) property.
MUTATIONS = [
    pytest.param(FLASH, ("reset_after_flash",), says_reset_default_is_false, (), r"\bdefault false\b", "Default true", id="true-false:reset-default"),
    pytest.param(FLASH, ("reset_after_flash",), says_true_resets, (), r"\breset the board after flashing\b", "True never resets the board after flashing", id="true-false:reset-effect"),
    pytest.param(LAST_REPORT, None, says_outer_ok_even_for_a_failed_report, (), r"\bok stays true\b", "ok is false", id="true-false:outer-ok"),
    pytest.param(FLASH, None, needs_with_refusal, ("allow_flash",), r"\bneeds allow_flash, else\b", "allow_flash true:", id="required-optional:allow_flash-granted"),
    pytest.param(FLASH, None, needs_with_refusal, ("allow_flash",), r"\bneeds allow_flash\b", "Needs no allow_flash", id="required-optional:allow_flash-waived"),
    pytest.param(UPLOAD, None, needs_with_refusal, ("allow_upload",), r"\bneeds (?:artifacts\.)?allow_upload\b", "allow_upload is optional", id="required-optional:allow_upload"),
    pytest.param(FLASH, ("reset_after_flash",), needs_with_refusal, ("allow_reset",), r"\bneeds allow_reset\b", "needs no allow_reset", id="required-optional:allow_reset"),
    pytest.param(UPLOAD, None, says_content_addressed, (), r"\bbytes and extension\b", "bytes or extension", id="and-or:content-address"),
    pytest.param(FLASH, None, says_one_image_input, (), r"\bnot with image_path\b", "with image_path, both required", id="and-or:image-input"),
    pytest.param(LAST_REPORT, None, says_how_to_judge_the_stored_verdict, (), r"\bjudge report\.ok\b[^.]*", "judge report.ok alone", id="and-or:stored-verdict"),
    pytest.param(CLASSIFY, None, says_record_outlives_successes, (), r"\bkeeps that failure through later successes\b", "clears that failure at the next success", id="negated-effect:record-cleared"),
    pytest.param(LAST_REPORT, None, says_touches_no_board, (), r"\bneeds no board\b", "Needs a board", id="negated-effect:report-needs-board"),
    pytest.param(UPLOAD, None, says_touches_no_board, (), r"\bno board needed\b", "needs a board", id="negated-effect:upload-needs-board"),
    pytest.param(FLASH, None, says_recovery_is_attempted_not_promised, (), r"\bmay try\b", "always does", id="negated-effect:recovery-promised"),
    pytest.param(FLASH, ("capture",), says_capture_failure_keeps_the_image, (), r"\bimage written\b", "nothing written", id="negated-effect:capture-image"),
    pytest.param(LAST_REPORT, None, says_reading_changes_nothing, (), r"\bchanges nothing\b", "clears it", id="negated-effect:report-read"),
    pytest.param(FLASH, None, says_every_call_flashes_again, (), r"\bevery call writes it again\b", "a repeated call skips an unchanged image", id="negated-effect:flash-again"),
    pytest.param(LAST_REPORT, None, says_newest_report_may_be_the_recovery, (), r"\bit can be\b", "it is never", id="negated-effect:recovery-report"),
    pytest.param(CLASSIFY, None, says_some_refusals_record_none, (), r"\bflash refusals by a debugger permission, such as ([^,]*), record nothing\b", r"All refusals, \1 included, are recorded", id="negated-effect:refusals-recorded"),
    pytest.param(FLASH, None, says_debug_session_makes_flash_busy, (), r"\bresource_busy: debug_stop_session first\b", "It stops any debug session itself", id="negated-effect:debug-session"),
    pytest.param(UPLOAD, ("data_base64",), says_size_limit, (), r"\bdecoded\b", "Encoded", id="size:encoded"),
    pytest.param(UPLOAD, ("data_base64",), says_size_limit, (), r"\bMiB\b", "bytes", id="size:unit"),
    pytest.param(UPLOAD, ("data_base64",), says_size_limit, (), r", else artifact_too_large", ", below it artifact_too_large", id="size:direction"),
    pytest.param(FLASH, None, says_timeout_bound, (), r"\beach debugger command\b", "the whole call", id="timeout:whole-call"),
    pytest.param(FLASH, None, says_timeout_bound, (), r"\bseconds\b", "ms", id="timeout:unit"),
    pytest.param(FLASH, ("image_path",), says_bin_needs_flash_address, (), r"\bevery backend\b", "pyOCD and STM32CubeProgrammer", id="backend:bin-address"),
    pytest.param(FLASH, ("image_path",), says_pyocd_does_not_verify, (), r"\bpyOCD does not verify\b", "OpenOCD does not verify", id="backend:verify"),
    pytest.param(FLASH, None, says_what_it_writes_and_through_what, (), r"\bnot st-flash\b", "OpenOCD only, not st-flash", id="backend:restriction"),
    pytest.param(LAST_REPORT, None, says_when_nothing_is_stored, (), r"\bnone yet: report_not_found\b", "None yet or unreadable: report_not_found", id="read-failure:merged"),
    pytest.param(CLASSIFY, None, says_when_nothing_is_stored, (), r"\bno failure yet: report_not_found\b", "no failure yet or a damaged record: report_not_found", id="read-failure:merged-classify"),
    pytest.param(FLASH, ("capture", "until"), says_until_limits, (), r"\b256 characters\b", "256 bytes", id="capture:until-unit"),
    pytest.param(FLASH, ("capture", "wait_timeout_s"), says_wait_bounds, (), r"\bseconds\b", "Milliseconds", id="capture:wait-unit"),
]


def mutated(tool: dict, pattern: str, replacement: str) -> tuple[dict, int]:
    """`tool` with `pattern` replaced in every description it carries, and how many replacements were made."""
    count = 0

    def walk(node: object) -> object:
        nonlocal count
        if isinstance(node, dict):
            copied = {}
            for key, value in node.items():
                if key == "description" and isinstance(value, str):
                    value, replaced = re.subn(pattern, replacement, value, flags=re.IGNORECASE)
                    count += replaced
                    copied[key] = value
                else:
                    copied[key] = walk(value)
            return copied
        if isinstance(node, list):
            return [walk(value) for value in node]
        return node

    return walk(tool), count


def scoped(tool: dict, scope: tuple[str, ...] | None) -> str:
    if scope is None:
        return definition_text(tool)
    if scope == ("description",):
        return str(tool["description"])
    return nested_text(tool, *scope)


@pytest.mark.parametrize(("name", "scope", "check", "arguments", "pattern", "replacement"), MUTATIONS)
def test_each_check_refuses_the_listed_definition_with_its_meaning_flipped(listed: dict[str, dict], name: str, scope, check, arguments: tuple, pattern: str, replacement: str) -> None:
    """The check holds for the definition as listed, and the same definition
    with one meaning flipped fails it: a check that only looks for words would
    pass both."""
    original = scoped(listed[name], scope)
    assert check(original, *arguments), original
    flipped, count = mutated(listed[name], pattern, replacement)
    assert count >= 1, (pattern, original)
    text = scoped(flipped, scope)
    assert not check(text, *arguments), text


# ---------------------------------------------------------------------------
# Every identifier a definition names is one the server really has.


def config_vocabulary() -> set[str]:
    """Every key the configuration schema lets an operator write, at any depth."""
    schema_path = Path(agentic_hil.__file__).resolve().parent / "schemas" / "config.schema.json"
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                found.update(properties)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(schema_path.read_text(encoding="utf-8")))
    return found


def test_every_identifier_the_definitions_name_is_real(listed: dict[str, dict]) -> None:
    """A snake_case word in any of the four definitions, at any depth, is a
    listed tool, an input property of a listed tool at any depth, a
    configuration key, or a string the server answers with. Quoted literals are
    example data and are not read."""
    properties = {path[-1] for tool in listed.values() for path, _ in described_schemas(tool["inputSchema"])}
    vocabulary = set(listed) | properties | config_vocabulary() | answer_vocabulary()
    assert {"max_upload_size_mb", "allow_upload", "allowed_roots", "source_tool", "likely_causes", "max_buffer_bytes"} <= vocabulary, "the vocabulary is read whole"
    assert "artifact_unknown" not in vocabulary, "a near miss is still refused"

    unknown = {name: sorted(set(SNAKE_CASE.findall(re.sub(QUOTED, " ", every_description(listed[name])))) - vocabulary) for name in TOOLS}
    assert not any(unknown.values()), unknown


# ---------------------------------------------------------------------------
# flash_firmware, as the code answers it today.


def test_a_flash_runs_in_its_own_run_and_leaves_the_board_unreset_by_default(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws")
    try:
        flashed = flash(service)
        assert flashed["ok"] is True, flashed
        assert flashed["run"]["implicit"] is True, flashed
        assert flashed["reset_after_flash"] is False, flashed
        assert "not reset" in flashed["summary"], flashed
        assert flashed["report_path"], flashed

        reset = flash(service, image_path=IMAGE, reset_after_flash=True)
        assert reset["ok"] is True, reset
        assert reset["reset_after_flash"] is True, reset
    finally:
        close(service)


def test_every_flash_call_programs_the_board_again(tmp_path: Path) -> None:
    """The same image twice is two writes, each committed and each logged."""
    workspace = tmp_path / "ws"
    service = new_service(workspace)
    try:
        first = flash(service)
        second = flash(service)
    finally:
        close(service)
    for flashed in (first, second):
        assert flashed["ok"] is True, flashed
        assert flashed["side_effect_status"] == "committed", flashed
        assert "program" in (workspace / flashed["log_path"]).read_text(encoding="utf-8"), flashed
    assert first["log_path"] != second["log_path"], (first, second)


@pytest.mark.parametrize(
    ("grant", "arguments"),
    [
        pytest.param("allow_flash", {"image_path": IMAGE}, id="allow_flash"),
        pytest.param("allow_reset", {"image_path": IMAGE, "reset_after_flash": True}, id="allow_reset-with-reset_after_flash"),
    ],
)
def test_a_flash_without_its_grant_is_refused_naming_the_permission(tmp_path: Path, grant: str, arguments: dict) -> None:
    service = new_service(tmp_path / "ws", permissions={**DEFAULT_TEST_PERMISSIONS, grant: False})
    try:
        refused = flash(service, **arguments)
        assert refused["error_type"] == "permission_denied", refused
        assert refused["permission"].endswith(f".permissions.{grant}"), refused
    finally:
        close(service)


@pytest.mark.parametrize("interlock", ["allow_raw_debugger_commands", "allow_mass_erase"])
@pytest.mark.parametrize("debugger_type", ["openocd", "stlink", "pyocd"])
def test_a_flash_is_refused_while_an_exclusive_grant_is_on(tmp_path: Path, debugger_type: str, interlock: str) -> None:
    """Every backend refuses to flash while raw commands or a mass erase are
    allowed, and the refusal names that grant, not allow_flash."""
    service = new_service(tmp_path / "ws", debugger_type=debugger_type, permissions={**DEFAULT_TEST_PERMISSIONS, interlock: True})
    try:
        refused = flash(service)
    finally:
        close(service)
    assert refused["error_type"] == "permission_denied", refused
    assert refused["permission"].endswith(f".permissions.{interlock}"), refused
    assert "recovery" not in refused, refused


def test_allow_reset_is_needed_only_for_reset_after_flash(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws", permissions={**DEFAULT_TEST_PERMISSIONS, "allow_reset": False})
    try:
        assert flash(service)["ok"] is True
    finally:
        close(service)


def test_a_flash_with_no_debugger_bound_is_not_supported(tmp_path: Path) -> None:
    service = edited_service(tmp_path / "ws", without_debuggers)
    try:
        refused = flash(service)
    finally:
        close(service)
    assert refused["error_type"] == "not_supported", refused


def test_a_debug_session_on_the_probe_makes_a_flash_busy_until_it_stops(tmp_path: Path) -> None:
    service = debug_service(tmp_path / "ws")
    try:
        started = call(service, "debug_start_session", {"image_path": IMAGE, "mode": "attach", "timeout_s": START_TIMEOUT_S})
        assert started["ok"] is True, started

        busy = flash(service)
        assert busy["error_type"] == "resource_busy", busy
        assert busy["retry_safe"] is True, busy

        assert call(service, "debug_stop_session", {})["ok"] is True
        assert flash(service)["ok"] is True
    finally:
        close(service)


def test_a_probe_held_by_another_owner_answers_device_busy(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws")
    stranger = BenchMutex(frontend="stranger", label="other-bench-session")
    stranger.acquire(list(debugger_device(service.config).lock_keys))
    try:
        busy = flash(service)
    finally:
        stranger.release_all()
        close(service)
    assert busy["error_type"] == "device_busy", busy["error_type"]
    assert busy["holder"]["label"] == "other-bench-session", busy["holder"]
    assert busy["retry_safe"] is True
    assert busy["side_effect_committed"] is False


def test_a_flash_needs_no_declared_run_but_a_declared_run_must_name_the_debugger(tmp_path: Path) -> None:
    """Standalone, the flash opens its own run. Inside a run that declared only
    some other board, the debugger is undeclared and the flash is refused."""
    service = new_service(tmp_path / "ws")
    try:
        standalone = flash(service)
        assert standalone["ok"] is True, standalone
        assert standalone["run"]["implicit"] is True, standalone

        service.coordinator.begin_run(["physical:unrelated-board"], label="unrelated-plan")
        try:
            refused = flash(service)
        finally:
            service.coordinator.end_run()
        assert refused["error_type"] == "undeclared_device", refused
        assert refused["side_effect_committed"] is False, refused

        assert call(service, "bench_run_start", {"devices": [{"kind": "debugger"}]})["ok"] is True
        try:
            declared = flash(service)
        finally:
            call(service, "bench_run_stop", {})
        assert declared["ok"] is True, declared
        assert "run" not in declared, declared
    finally:
        close(service)


@pytest.mark.parametrize(
    ("arguments", "error_type"),
    [
        pytest.param({"image_path": "other/app.elf"}, "artifact_validation_failed", id="outside-the-allowed-roots"),
        pytest.param({"image_path": "build/app.txt"}, "artifact_validation_failed", id="extension-not-allowed"),
        pytest.param({"image_path": "build/bad.elf"}, "artifact_validation_failed", id="not-an-elf"),
        pytest.param({"image_path": "build/none.elf"}, "artifact_not_found", id="missing-file"),
        pytest.param({"image_path": "build/big.bin"}, "artifact_too_large", id="over-max_upload_size_mb"),
        pytest.param({"artifact_id": UNKNOWN_ID}, "artifact_not_found", id="unknown-artifact_id"),
        pytest.param({"image_path": IMAGE, "artifact_id": UNKNOWN_ID}, "invalid_argument", id="both-inputs"),
        pytest.param({"reset_after_flash": False}, "invalid_argument", id="neither-input"),
    ],
)
def test_an_image_the_server_may_not_flash_is_refused_by_name(tmp_path: Path, arguments: dict, error_type: str) -> None:
    workspace = tmp_path / "ws"
    service = new_service(workspace)
    (workspace / "other").mkdir()
    (workspace / "other" / "app.elf").write_bytes(ELF)
    (workspace / "build" / "app.txt").write_bytes(ELF)
    (workspace / "build" / "bad.elf").write_bytes(b"not an elf")
    (workspace / "build" / "big.bin").write_bytes(b"\x01" * (TEST_MAX_UPLOAD_BYTES + 1))
    try:
        refused = flash(service, **arguments)
        assert refused["error_type"] == error_type, refused
        if error_type == "artifact_too_large":
            assert refused["max_bytes"] == TEST_MAX_UPLOAD_BYTES, refused
    finally:
        close(service)


@pytest.mark.parametrize(
    ("debugger_type", "verified", "bin_needs_flash_address"),
    [
        pytest.param("openocd", True, True, id="openocd"),
        pytest.param("stlink", True, True, id="stm32cubeprogrammer"),
        pytest.param("pyocd", False, True, id="pyocd"),
    ],
)
def test_each_backend_verifies_and_takes_a_bin_as_the_definition_says(tmp_path: Path, debugger_type: str, verified: bool, bin_needs_flash_address: bool) -> None:
    """OpenOCD and STM32CubeProgrammer verify what they wrote, pyOCD does not;
    a .bin without flash_address is refused before anything is sent, on every
    backend (#680)."""
    service = new_service(tmp_path / "ws", debugger_type=debugger_type)
    try:
        flashed = flash(service)
        assert flashed["ok"] is True, flashed
        assert flashed["verify"] is verified, flashed
        binary = flash(service, image_path=BINARY)
    finally:
        close(service)
    if bin_needs_flash_address:
        assert binary["error_type"] == "invalid_argument", binary
        assert "flash_address" in binary["summary"], binary
        assert binary.get("side_effect_status", "not_started") == "not_started", binary
    else:
        assert binary["ok"] is True, binary

    addressed = new_service(tmp_path / "addressed", debugger_type=debugger_type, flash_address="0x08000000")
    try:
        assert flash(addressed, image_path=BINARY)["ok"] is True
    finally:
        close(addressed)


def test_the_allowed_roots_are_build_when_the_key_is_unset(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    service = new_service(workspace, omit_allowed_roots=True)
    (workspace / "other").mkdir()
    (workspace / "other" / "app.elf").write_bytes(ELF)
    try:
        assert flash(service)["ok"] is True
        assert flash(service, image_path="other/app.elf")["error_type"] == "artifact_validation_failed"
    finally:
        close(service)


def test_the_configuration_defaults_the_definitions_name(tmp_path: Path) -> None:
    """A debugger entry and an artifacts section that leave the keys out."""
    path = write_config(tmp_path / "ws", omit_allowed_roots=True)
    text = path.read_text(encoding="utf-8")
    text, timeouts = re.subn(r"(?m)^[ \t]+timeout_s: 5\n", "", text)
    text, limits = re.subn(r"(?m)^[ \t]+max_upload_size_mb: 1\n", "", text)
    text, extensions = re.subn(r"(?m)^[ \t]+allowed_extensions: .*\n", "", text)
    assert (timeouts, limits, extensions) == (1, 1, 1), text
    path.write_text(text, encoding="utf-8")
    config = load_config(str(path))
    assert config.debugger.timeout_s == DEFAULT_DEBUGGER_TIMEOUT_S
    assert config.artifacts.max_upload_size_mb == DEFAULT_MAX_UPLOAD_SIZE_MB
    assert tuple(config.artifacts.allowed_extensions) == DEFAULT_EXTENSIONS
    assert [Path(root).name for root in config.artifacts.allowed_roots] == [DEFAULT_ALLOWED_ROOT]


def test_a_flash_is_bounded_by_the_debugger_timeout(tmp_path: Path) -> None:
    config = load_config(str(config_with_debugger(tmp_path / "ws", FAKE_HUNG_DEBUGGER)))
    (tmp_path / "ws" / "build").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ws" / IMAGE).write_bytes(ELF)
    service = AgenticHILToolService(config, frontend="mcp")
    started = time.monotonic()
    try:
        timed_out = flash(service)
    finally:
        close(service)
    assert timed_out["error_type"] == "timeout", timed_out
    assert timed_out["tool"] == FLASH, timed_out
    assert time.monotonic() - started < CALL_CEILING_S


def test_the_debugger_timeout_bounds_each_command_not_the_whole_call(tmp_path: Path) -> None:
    """pyOCD flashes and then resets in two commands. Each takes 2 s against a
    3.5 s timeout_s: the call succeeds although it runs longer than timeout_s,
    because the bound applies to each command on its own."""
    timeout_s = 3.5
    delay_s = 2.0
    fake = tmp_path / "slow_pyocd.py"
    fake.write_text(SLOW_PYOCD.format(delay_s=delay_s, fake=str(FAKE_PYOCD)), encoding="utf-8")
    service = new_service(tmp_path / "ws", debugger_type="pyocd", debugger_executable=fake, timeout_s=timeout_s)
    started = time.monotonic()
    try:
        flashed = flash(service, image_path=IMAGE, reset_after_flash=True)
    finally:
        close(service)
    elapsed = time.monotonic() - started
    assert flashed["ok"] is True, flashed
    assert flashed["reset_after_flash"] is True, flashed
    assert elapsed > timeout_s, elapsed


def test_a_failed_flash_is_followed_by_a_recovery_reset_into_halt(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws", debugger_executable=FAKE_OPENOCD_ERASE_REFUSED)
    try:
        failed = flash(service)
    finally:
        close(service)
    assert failed["ok"] is False, failed
    assert failed["run"]["aborted"] is True, failed
    assert failed["recovery"]["attempted"] is True, failed
    assert "reset_halt" in failed["recovery"]["actions"], failed
    assert failed["recovery"]["outcome"] == "recovered", failed


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        pytest.param({"auto_recover": "off"}, "auto_recover_policy_off", id="auto_recover-off"),
        pytest.param({"auto_recover": "readonly"}, None, id="auto_recover-readonly"),
        pytest.param({"permissions": {**DEFAULT_TEST_PERMISSIONS, "allow_reset": False}}, None, id="allow_reset-off"),
    ],
)
def test_the_recovery_reset_follows_the_policy_and_the_reset_grant(tmp_path: Path, kwargs: dict, reason: str | None) -> None:
    service = new_service(tmp_path / "ws", debugger_executable=FAKE_OPENOCD_ERASE_REFUSED, **kwargs)
    try:
        failed = flash(service)
    finally:
        close(service)
    assert failed["ok"] is False, failed
    assert "reset_halt" not in (failed.get("recovery") or {}).get("actions", []), failed
    if reason is not None:
        assert failed["recovery"]["attempted"] is False, failed
        assert failed["recovery"]["reason_not_attempted"] == reason, failed


def test_a_refused_flash_starts_no_recovery(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws", permissions={**DEFAULT_TEST_PERMISSIONS, "allow_flash": False})
    try:
        refused = flash(service)
    finally:
        close(service)
    assert refused["error_type"] == "permission_denied", refused
    assert "recovery" not in refused, refused
    assert refused.get("side_effect_status", "not_started") == "not_started", refused


def test_a_recovery_reset_can_fail_too(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws", debugger_executable=FAKE_OPENOCD_NO_TARGET)
    try:
        failed = flash(service)
    finally:
        close(service)
    assert failed["recovery"]["attempted"] is True, failed
    assert failed["recovery"]["outcome"] == "failed", failed
    assert failed["recovery"]["failed_action"] == "reset_halt", failed


def test_inside_a_declared_run_the_recovery_waits_for_bench_run_stop(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws", debugger_executable=FAKE_OPENOCD_ERASE_REFUSED)
    try:
        assert call(service, "bench_run_start", {"devices": [{"kind": "debugger"}]})["ok"] is True
        failed = flash(service)
        stopped = call(service, "bench_run_stop", {})
    finally:
        close(service)
    assert failed["ok"] is False, failed
    assert "recovery" not in failed, failed
    assert stopped["recovery"]["attempted"] is True, stopped
    assert "reset_halt" in stopped["recovery"]["actions"], stopped


def test_a_failure_after_contact_is_not_a_refusal(tmp_path: Path) -> None:
    """An erase the target refused happened on the board: the effect is not
    known to be absent, so it is no retry-safe refusal."""
    service = new_service(tmp_path / "ws", debugger_executable=FAKE_OPENOCD_ERASE_REFUSED, auto_recover="off")
    try:
        failed = flash(service)
    finally:
        close(service)
    assert failed["ok"] is False, failed
    assert failed["error_type"] == "flash_erase_failed", failed
    assert failed.get("side_effect_status", "not_started") != "not_started", failed
    assert failed["retry_safe"] is False, failed


def test_a_reset_that_fails_after_a_good_flash_is_a_partial_effect(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws", debugger_type="pyocd", debugger_executable=FAKE_PYOCD_RESET_REFUSED)
    try:
        failed = flash(service, image_path=IMAGE, reset_after_flash=True)
    finally:
        close(service)
    assert failed["ok"] is False, failed
    assert failed["error_type"] == "reset_failed", failed
    assert failed["side_effect_committed"] is True, failed
    assert failed["side_effect_status"] == "partial", failed


def test_flashing_an_uploaded_artifact_needs_uploads_allowed(tmp_path: Path) -> None:
    service = edited_service(tmp_path / "ws", without_uploads)
    try:
        refused = flash(service, artifact_id=UNKNOWN_ID)
        assert refused["error_type"] == "permission_denied", refused
        assert refused["permission"] == "artifacts.allow_upload", refused
    finally:
        close(service)


# ---------------------------------------------------------------------------
# artifact_upload, as the code answers it today.


def test_an_upload_is_named_by_its_sha256_and_lowercased_extension(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    service = new_service(workspace)
    expected = hashlib.sha256(ELF).hexdigest() + ".elf"
    try:
        by_bytes = upload(service, filename="FW.ELF", data_base64=b64(ELF))
        assert by_bytes["ok"] is True, by_bytes
        assert by_bytes["artifact_id"] == expected, by_bytes
        assert (workspace / ".agentic-hil" / "artifacts" / expected).read_bytes() == ELF

        by_path = upload(service, image_path=IMAGE)
        assert by_path["artifact_id"] == expected, by_path

        spaced = upload(service, filename="fw.elf", data_base64="\n ".join(re.findall(".{1,8}", b64(ELF))))
        assert spaced["artifact_id"] == expected, spaced
    finally:
        close(service)


def test_the_same_bytes_under_another_extension_are_another_artifact(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    service = new_service(workspace)
    digest = hashlib.sha256(ELF).hexdigest()
    try:
        as_elf = upload(service, filename="fw.elf", data_base64=b64(ELF))
        as_bin = upload(service, filename="fw.bin", data_base64=b64(ELF))
    finally:
        close(service)
    assert (as_elf["artifact_id"], as_bin["artifact_id"]) == (f"{digest}.elf", f"{digest}.bin"), (as_elf, as_bin)
    for artifact_id in (as_elf["artifact_id"], as_bin["artifact_id"]):
        assert (workspace / ".agentic-hil" / "artifacts" / artifact_id).read_bytes() == ELF


def test_a_repeated_upload_answers_the_same_id_and_keeps_one_file(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    service = new_service(workspace)
    try:
        first = upload(service, image_path=IMAGE)
        second = upload(service, image_path=IMAGE)
    finally:
        close(service)
    assert first["ok"] is True and second["ok"] is True, (first, second)
    assert first["artifact_id"] == second["artifact_id"], (first, second)
    assert [path.name for path in (workspace / ".agentic-hil" / "artifacts").iterdir()] == [first["artifact_id"]]


@pytest.mark.parametrize(
    ("arguments", "error_type"),
    [
        pytest.param({"filename": "fw.elf", "data_base64": "abc"}, "invalid_argument", id="unpadded-base64"),
        pytest.param({"filename": "build/fw.elf", "data_base64": b64(ELF)}, "invalid_argument", id="filename-with-a-path"),
        pytest.param({"image_path": IMAGE, "data_base64": b64(ELF)}, "invalid_argument", id="both-inputs"),
        pytest.param({"filename": "fw.txt", "data_base64": b64(ELF)}, "artifact_validation_failed", id="extension-not-allowed"),
        pytest.param({"filename": "fw.elf", "data_base64": b64(b"not an elf")}, "artifact_validation_failed", id="not-an-elf"),
        pytest.param({"filename": "fw.bin", "data_base64": b64(b"\x01" * (TEST_MAX_UPLOAD_BYTES + 1))}, "artifact_too_large", id="over-max_upload_size_mb"),
        pytest.param({"image_path": "other/app.elf"}, "artifact_validation_failed", id="outside-the-allowed-roots"),
        pytest.param({"image_path": "build/none.elf"}, "artifact_not_found", id="missing-file"),
    ],
)
def test_an_upload_the_server_may_not_store_is_refused_by_name(tmp_path: Path, arguments: dict, error_type: str) -> None:
    workspace = tmp_path / "ws"
    service = new_service(workspace)
    (workspace / "other").mkdir()
    (workspace / "other" / "app.elf").write_bytes(ELF)
    try:
        refused = upload(service, **arguments)
        assert refused["error_type"] == error_type, refused
    finally:
        close(service)


def test_an_upload_onto_a_symlinked_destination_is_unsafe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stored file is never written through a link. Creating a symlink
    needs a privilege a Windows runner may lack, so the destination is made to
    read as one."""
    workspace = tmp_path / "ws"
    service = new_service(workspace)
    destination = workspace / ".agentic-hil" / "artifacts" / (hashlib.sha256(ELF).hexdigest() + ".elf")
    real_is_symlink = pathlib.Path.is_symlink
    monkeypatch.setattr(pathlib.Path, "is_symlink", lambda path: path == destination or real_is_symlink(path))
    try:
        refused = upload(service, filename="fw.elf", data_base64=b64(ELF))
    finally:
        close(service)
    assert refused["error_type"] == "unsafe_configured_path", refused
    assert not destination.exists()


def test_an_upload_needs_allow_upload(tmp_path: Path) -> None:
    service = edited_service(tmp_path / "ws", without_uploads)
    try:
        refused = upload(service, image_path=IMAGE)
        assert refused["error_type"] == "permission_denied", refused
        assert refused["permission"] == "artifacts.allow_upload", refused
    finally:
        close(service)


def test_an_upload_needs_no_debugger_and_writes_no_report(tmp_path: Path) -> None:
    service = edited_service(tmp_path / "ws", without_debuggers)
    try:
        assert flash(service)["error_type"] == "not_supported", "no debugger is configured"
        stored = upload(service, image_path=IMAGE)
        assert stored["ok"] is True, stored
        assert last_report(service)["error_type"] == "report_not_found"
    finally:
        close(service)


def test_an_uploaded_artifact_is_flashed_and_debugged_by_its_id(tmp_path: Path) -> None:
    service = debug_service(tmp_path / "ws")
    try:
        stored = upload(service, image_path=IMAGE)
        assert stored["ok"] is True, stored
        assert flash(service, artifact_id=stored["artifact_id"])["ok"] is True
        started = call(service, "debug_start_session", {"artifact_id": stored["artifact_id"], "mode": "load", "timeout_s": START_TIMEOUT_S})
        assert started["ok"] is True, started
        assert call(service, "debug_stop_session", {})["ok"] is True
    finally:
        close(service)


# ---------------------------------------------------------------------------
# get_last_report and classify_last_error, as the code answers them today.


def test_before_any_report_both_answer_report_not_found(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws")
    try:
        assert last_report(service)["error_type"] == "report_not_found"
        assert classify(service)["error_type"] == "report_not_found"
    finally:
        close(service)


def test_get_last_report_returns_the_flash_report_under_report_and_reading_changes_nothing(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws")
    try:
        flash(service)
        answer = last_report(service)
        assert answer["ok"] is True, answer
        assert answer["report"]["tool"] == FLASH, answer
        assert answer["report"]["ok"] is True, answer
        assert last_report(service) == answer
    finally:
        close(service)


def test_get_last_report_answers_ok_for_a_stored_failure(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws", debugger_executable=FAKE_OPENOCD_NO_TARGET)
    try:
        assert flash(service)["ok"] is False
        answer = last_report(service)
        assert answer["ok"] is True, answer
        assert answer["report"]["ok"] is False, answer
        assert answer["report"]["error_type"], answer
    finally:
        close(service)


def test_a_report_that_says_ok_can_still_have_failed_its_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The flash itself succeeded and its workspace report could not be
    written: the call answers ok true and is an error all the same, and the
    stored report says ok true beside audit_ok false and cleanup_required true.
    report.ok alone would read it as a success; classify_last_error does not.
    With recovery off, no recovery report replaces it."""
    service = new_service(tmp_path / "ws", auto_recover="off")
    real_write = report_module.safe_write_text

    def refuse_the_flash_report(config, path, document, *args, **kwargs):
        if '"tool": "flash_firmware"' in document:
            raise OSError(28, "No space left on device")
        return real_write(config, path, document, *args, **kwargs)

    try:
        monkeypatch.setattr(report_module, "safe_write_text", refuse_the_flash_report)
        flashed = flash(service)
        monkeypatch.setattr(report_module, "safe_write_text", real_write)
        stored = last_report(service)
        classified = classify(service)
    finally:
        close(service)
    assert flashed["ok"] is True, flashed
    assert flashed["audit_ok"] is False, flashed
    assert overall_success(flashed) is False, flashed
    assert stored["ok"] is True, stored
    assert stored["report"]["tool"] == FLASH, stored
    assert stored["report"]["ok"] is True, stored
    assert stored["report"]["audit_ok"] is False, stored
    assert stored["report"]["cleanup_required"] is True, stored
    assert classified["error_type"] == "audit_failed", classified
    assert classified["source_tool"] == FLASH, classified


def test_after_a_recovered_flash_the_newest_report_is_the_recovery_and_classify_names_the_flash(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws", debugger_executable=FAKE_OPENOCD_ERASE_REFUSED)
    try:
        failed = flash(service)
        assert failed["recovery"]["outcome"] == "recovered", failed
        newest = last_report(service)["report"]
        assert newest["tool"] == "probe_target", newest
        assert newest["ok"] is True, newest
        classified = classify(service)
        assert classified["source_tool"] == FLASH, classified
        assert classified["error_type"] == failed["error_type"], classified
    finally:
        close(service)


def test_when_the_recovery_reset_fails_too_classify_names_reset_target(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws", debugger_executable=FAKE_OPENOCD_NO_TARGET)
    try:
        failed = flash(service)
        assert failed["recovery"]["failed_action"] == "reset_halt", failed
        classified = classify(service)
        assert classified["source_tool"] == "reset_target", classified
        assert last_report(service)["report"]["tool"] == "reset_target"
    finally:
        close(service)


def test_classify_last_error_answers_with_the_fields_its_definition_names(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws", debugger_executable=FAKE_OPENOCD_NO_TARGET)
    try:
        flash(service)
        classified = classify(service)
    finally:
        close(service)
    assert classified["ok"] is True, classified
    for field in ("error_type", "summary", "source_tool", "report_path", "log_path"):
        assert classified.get(field), (field, classified)
    assert isinstance(classified["likely_causes"], list) and classified["likely_causes"], classified


def test_the_failure_record_outlives_a_later_success_and_a_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    service = new_service(workspace, debugger_executable=FAKE_OPENOCD_NO_TARGET)
    try:
        failed = flash(service)
        assert failed["ok"] is False, failed
    finally:
        close(service)
    restarted = new_service(workspace, debugger_executable=FAKE_OPENOCD)
    try:
        assert flash(restarted)["ok"] is True
        assert last_report(restarted)["report"]["ok"] is True
        classified = classify(restarted)
        assert classified["error_type"] == failed["error_type"], classified
    finally:
        close(restarted)


@pytest.mark.parametrize(
    ("permissions", "arguments"),
    [
        pytest.param({**DEFAULT_TEST_PERMISSIONS, "allow_flash": False}, {"image_path": IMAGE}, id="allow_flash-off"),
        pytest.param(None, {"image_path": IMAGE, "artifact_id": UNKNOWN_ID}, id="arguments-the-schema-refuses"),
        # The two permissions flashing is interlocked against refuse the flash
        # before the probe's lease as allow_flash does, so they leave no record
        # either (#679).
        pytest.param({**DEFAULT_TEST_PERMISSIONS, "allow_mass_erase": True}, {"image_path": IMAGE}, id="allow_mass_erase-on"),
        pytest.param({**DEFAULT_TEST_PERMISSIONS, "allow_raw_debugger_commands": True}, {"image_path": IMAGE}, id="allow_raw_debugger_commands-on"),
    ],
)
def test_some_refusals_record_no_report(tmp_path: Path, permissions: dict | None, arguments: dict) -> None:
    service = new_service(tmp_path / "ws", permissions=permissions)
    try:
        refused = flash(service, **arguments)
        assert refused["ok"] is False, refused
        assert "report_path" not in refused, refused
        assert last_report(service)["error_type"] == "report_not_found"
        assert classify(service)["error_type"] == "report_not_found"
    finally:
        close(service)


@pytest.mark.parametrize("permission", ["allow_mass_erase", "allow_raw_debugger_commands"])
def test_an_interlock_refusal_takes_no_lease(tmp_path: Path, permission: str) -> None:
    """The refusal comes before the probe is leased, as `debug_start_session`'s
    does: no lease is taken and nothing is started (#679)."""
    service = new_service(tmp_path / "ws", permissions={**DEFAULT_TEST_PERMISSIONS, permission: True})
    leased: list[str] = []
    coordinated = service._coordinated_debug_call

    def watch(name, callback):
        leased.append(name)
        return coordinated(name, callback)

    service._coordinated_debug_call = watch
    try:
        refused = flash(service, image_path=IMAGE, reset_after_flash=True)
    finally:
        close(service)
    assert refused["error_type"] == "permission_denied", refused
    assert refused["permission"].endswith(f".permissions.{permission}"), refused
    assert refused.get("side_effect_status", "not_started") == "not_started", refused
    assert leased == [], leased


@pytest.mark.parametrize("tool", REPORT_TOOLS)
def test_a_malformed_report_state_answers_report_state_damaged(tmp_path: Path, tool: str) -> None:
    service = new_service(tmp_path / "ws", debugger_executable=FAKE_OPENOCD_NO_TARGET)
    try:
        flash(service)
        Path(report_state_path(service.config)).write_text("{not json", encoding="utf-8")
        answer = call(service, tool, {})
    finally:
        close(service)
    assert answer["ok"] is False, answer
    assert answer["error_type"] == "report_state_damaged", answer


@pytest.mark.parametrize("tool", REPORT_TOOLS)
def test_an_unreadable_report_state_answers_report_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    service = new_service(tmp_path / "ws", debugger_executable=FAKE_OPENOCD_NO_TARGET)

    def unreadable(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    try:
        flash(service)
        monkeypatch.setattr(report_module, "safe_read_text", unreadable)
        answer = call(service, tool, {})
        monkeypatch.undo()
    finally:
        close(service)
    assert answer["ok"] is False, answer
    assert answer["error_type"] == "report_unreadable", answer


def test_the_report_tools_need_no_debugger(tmp_path: Path) -> None:
    """A failure recorded with a debugger configured is still read, and still
    classified, once the configuration names none."""
    workspace = tmp_path / "ws"
    service = new_service(workspace, debugger_executable=FAKE_OPENOCD_NO_TARGET)
    try:
        failed = flash(service)
    finally:
        close(service)
    unbound = edited_service(workspace, without_debuggers)
    try:
        assert flash(unbound)["error_type"] == "not_supported", "no debugger is configured"
        assert last_report(unbound)["report"]["ok"] is False
        assert classify(unbound)["error_type"] == failed["error_type"]
    finally:
        close(unbound)
