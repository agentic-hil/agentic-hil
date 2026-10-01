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
relation checks are themselves run against paraphrases they must accept and
inversions they must reject. Every identifier a definition names is one the
server lists, configures or can answer with.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from pathlib import Path

import pytest
from conftest import (
    DEFAULT_TEST_PERMISSIONS,
    FAKE_OPENOCD,
    FAKE_OPENOCD_ERASE_REFUSED,
    FAKE_OPENOCD_NO_TARGET,
    write_config,
)
from test_debug_sessions import START_TIMEOUT_S, debug_service
from test_debugger_processes import CALL_CEILING_S, FAKE_HUNG_DEBUGGER, config_with_debugger
from test_read_until import close
from test_tool_definition_uart import (
    NEGATION,
    QUOTED,
    SNAKE_CASE,
    answer_vocabulary,
    call,
    claims,
    clauses,
    definition_text,
    names,
    one_of,
    property_text,
    sentences,
)

import agentic_hil
from agentic_hil.config import load_config
from agentic_hil.mcp import handle_mcp_message
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

# The smallest image the validator accepts as an ELF: the magic, then padding.
ELF = b"\x7fELF" + b"\x00" * 60
IMAGE = "build/app.elf"


# ---------------------------------------------------------------------------
# Services and calls.


def new_service(workspace: Path, **kwargs: object) -> AgenticHILToolService:
    """A server on the fake OpenOCD, with `build/app.elf` in its workspace."""
    path = write_config(workspace, **kwargs)
    (workspace / "build").mkdir(parents=True, exist_ok=True)
    (workspace / IMAGE).write_bytes(ELF)
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


# The relations, each a predicate over a definition's text, so the paraphrase
# test at the end of this section can run them on sentences of its own.

DEFAULT_FALSE = r"\bdefaults?\b[^.;]*\bfalse\b|\bfalse\b[^.;]*\bdefault\b"
DEFAULT_TRUE = r"\bdefaults?\b[^.;]*\btrue\b|\btrue\b[^.;]*\bdefault\b"
# The result named `timeout`, not the configuration key `timeout_s`.
TIMEOUT_RESULT = r"(?<![A-Za-z0-9_])timeout(?![A-Za-z0-9_])"
NEGATED_NEED = r"\b(?:needs?|requires?)\s+no\b|\b(?:does\s+not|doesn't|never)\s+(?:need|require)|\bnot\s+(?:needed|required)\b|\boptional\b"


def needs_with_refusal(text: str, key: str, refusal: str = "permission_denied") -> bool:
    """`key` is needed, and its absence answers `refusal`, in one clause; never "needs no `key`"."""
    needed = one_of(clauses(text), rf"\b{key}\b", rf"\b{refusal}\b")
    waived = any(re.search(rf"\b{key}\b", part) and re.search(NEGATED_NEED, part, re.IGNORECASE) for part in clauses(text))
    return needed and not waived


def says_debug_session_makes_flash_busy(text: str) -> bool:
    """A debug session on the probe answers resource_busy until debug_stop_session; the flash never ends it itself."""
    busy = one_of(sentences(text), r"\bresource_busy\b", r"\bdebug_stop_session\b", r"(?<![A-Za-z0-9_])session\b")
    takes_over = claims(text, r"\b(?:ends|stops|closes|replaces|takes over)\s+(?:the|a|any)\s+(?:active\s+|running\s+)?debug session")
    return busy and not takes_over


def says_reset_default_is_false(text: str) -> bool:
    """reset_after_flash defaults to false and false does not reset; never a true default."""
    default = one_of(clauses(text), DEFAULT_FALSE)
    unreset = one_of(clauses(text), r"\bfalse\b|\bdefault\b", rf"{NEGATION}[^.;]*\breset\b|\breset\b[^.;]*{NEGATION}|\bunreset\b")
    return default and unreset and not claims(text, DEFAULT_TRUE)


def says_failed_flash_ends_in_a_recovery_reset(text: str) -> bool:
    """A failed flash is followed by the recovery's reset into halt, under the
    recovery.auto_recover policy that decides it; never "a failed flash leaves
    the board untouched"."""
    followed = one_of(sentences(text), r"\bfail", r"\brecover", r"\breset\b", r"\bhalt", r"\bauto_recover\b")
    untouched = claims(text, r"\bfail\w*\b[^.;]*\b(?:untouched|unchanged|as it was|not reset)\b")
    return followed and not untouched


def says_timeout_bound(text: str) -> bool:
    """timeout_s bounds the call, 60 by default, and running out answers `timeout`; 60 is no ceiling."""
    bounded = one_of(clauses(text), r"\btimeout_s\b", TIMEOUT_RESULT)
    default = one_of(clauses(text), r"\btimeout_s\b", rf"\b{DEFAULT_DEBUGGER_TIMEOUT_S}\b", r"\bdefault")
    ceiling = claims(text, rf"\b(?:at most|up to|maximum(?: of)?|no more than)\s+{DEFAULT_DEBUGGER_TIMEOUT_S}\b")
    return bounded and default and not ceiling


def says_one_image_input(text: str) -> bool:
    """image_path or artifact_id, exactly one; never both together."""
    either = one_of(clauses(text), r"\bimage_path\b", r"\bartifact_id\b", r"\bnot both\b|\bexactly one\b|\bone of\b|\beither\b|\binstead\b")
    both = claims(text, r"\bboth\b[^.;]*\b(?:required|needed|must)\b|\b(?:requires?|needs?)\s+both\b")
    return either and not both


def says_content_addressed(text: str) -> bool:
    """The id is the sha256 of the bytes plus the extension, so the same bytes give the same id; never random."""
    digest = one_of(clauses(text), r"\bsha-?256\b", r"\bextension\b|\bsuffix\b")
    stable = one_of(clauses(text), r"\bsame\b|\bidentical\b|\bequal\b", r"\bbytes\b|\bcontents?\b", r"\bid\b|\bartifact_id\b")
    random = claims(text, r"\brandom\b|\buuid\b|\bnew id (?:each|every)\b|\bunique (?:per|for each) upload\b")
    return digest and stable and not random


def says_whitespace_ignored(text: str) -> bool:
    ignored = one_of(clauses(text), r"\bwhitespace\b|\bspaces?\b|\bline breaks?\b|\bnewlines?\b", r"\bignored\b|\bremoved\b|\bskipped\b|\bstripped\b|\ballowed\b")
    refused = claims(text, r"\b(?:whitespace|spaces?|line breaks?|newlines?)\b[^.;]*\b(?:invalid|not allowed|refused|rejected|forbidden)\b")
    return ignored and not refused


def says_size_limit(text: str) -> bool:
    """max_upload_size_mb, 64 by default, over it artifact_too_large; 64 is no fixed ceiling."""
    limit = one_of(clauses(text), r"\bmax_upload_size_mb\b", rf"\b{DEFAULT_MAX_UPLOAD_SIZE_MB}\b", r"\bdefault")
    refusal = one_of(clauses(text), r"\bmax_upload_size_mb\b", r"\bartifact_too_large\b")
    ceiling = claims(text, rf"\b(?:at most|up to|maximum(?: of)?|no more than)\s+{DEFAULT_MAX_UPLOAD_SIZE_MB}\b")
    return limit and refusal and not ceiling


def says_bare_filename(text: str) -> bool:
    """A bare file name, no path, whose extension sets the id's suffix."""
    bare = one_of(clauses(text), rf"\bbare\b|{NEGATION}", r"\bpaths?\b|\bdirector|\bseparators?\b|/")
    suffix = one_of(clauses(text), r"\bextension\b|\bsuffix\b", r"\bartifact_id\b|\bid\b")
    return bare and suffix


def says_touches_no_board(text: str) -> bool:
    """No board, probe or debugger is involved; never "needs a debugger"."""
    untouched = one_of(clauses(text), NEGATION, r"\bboard\b|\bdebugger\b|\bhardware\b|\bprobe\b|\btarget\b")
    needs = claims(text, r"\b(?:needs?|requires?)\s+(?:a|an|the)\s+(?:bound\s+|configured\s+|connected\s+)?(?:debugger|probe|board|target)\b")
    return untouched and not needs


def says_outer_ok_even_for_a_failed_report(text: str) -> bool:
    """get_last_report answers ok true even when the stored report failed; the stored verdict is report.ok."""
    true_anyway = one_of(clauses(text), r"(?<![A-Za-z0-9_.])ok(?![A-Za-z0-9_.])", r"\btrue\b", r"\bfail")
    inverted = one_of(clauses(text), r"(?<![A-Za-z0-9_.])ok(?![A-Za-z0-9_.])[^.;]*\bfalse\b", r"\bfail")
    return true_anyway and claims(text, r"\breport\.(?:ok|error_type)\b") and not inverted


def says_newest_report_may_be_the_recovery(text: str) -> bool:
    """After a failed call the newest report can be the recovery's reset_target or probe_target."""
    return one_of(sentences(text), r"\brecover", r"\breset_target\b|\bprobe_target\b") and claims(text, r"\breport\.tool\b")


def says_record_outlives_successes(text: str) -> bool:
    """The failure record stays until a newer failure: a success does not clear it."""
    stays = one_of(clauses(text), r"\bsuccess|\bsucceed", r"\bstays?\b|\bremains?\b|\bkept\b|\bkeeps?\b|\bpersists?\b|\bsurvives?\b|\buntil\b")
    # Within one phrase: "a success clears it", "cleared by a success", not a
    # later phrase of the same sentence that names the recovery reset.
    cleared = claims(
        text,
        r"\b(?:clear|clears|cleared|reset|resets|erase|erases|erased|forgotten|replaced|overwritten)\b[^.;:,]*\bsuccess"
        r"|\bsuccess\w*\b[^.;:,]*\b(?:clears?|cleared|resets?|erases?|erased|replaces?|overwrites?)\b",
    )
    return stays and not cleared


def says_some_refusals_record_none(text: str) -> bool:
    """The allow_flash refusal (and a schema refusal) leave no record; never "every call is recorded"."""
    none = one_of(clauses(text), r"\brefus|\bpermission_denied\b|\binvalid_argument\b", r"\ballow_flash\b", r"\bno\b|\bnone\b|\bnot\b|\bnothing\b|\bwithout\b")
    every = claims(text, r"\b(?:every|each|all)\b[^.;]*\b(?:calls?|refusals?|failures?|errors?)\b[^.;]*\b(?:recorded|writes?|written|records?)\b")
    return none and not every


def says_when_nothing_is_stored(text: str) -> bool:
    return one_of(clauses(text), r"\breport_not_found\b", r"\bbefore\b|\bno\b|\bnot yet\b|\bnone\b|\bnothing\b|\byet\b")


def says_source_may_be_the_recovery_reset(text: str) -> bool:
    return one_of(sentences(text), r"\bsource_tool\b", r"\breset_target\b", r"\bflash_firmware\b|\brecover")


def test_the_four_tools_are_listed_with_a_description_within_the_budget(listed: dict[str, dict]) -> None:
    for name in TOOLS:
        assert name in listed, sorted(listed)
        description = listed[name].get("description")
        assert isinstance(description, str) and description.strip(), name
        assert len(description) <= DESCRIPTION_LIMIT, (name, len(description))


@pytest.mark.parametrize("name", TOOLS)
def test_every_input_property_describes_itself(listed: dict[str, dict], name: str) -> None:
    properties = listed[name]["inputSchema"]["properties"]
    undescribed = sorted(key for key in properties if not property_text(listed[name], key).strip())
    assert not undescribed, f"{name}: input properties without a description: {undescribed}"
    oversized = sorted(key for key in properties if len(property_text(listed[name], key)) > PROPERTY_DESCRIPTION_LIMIT)
    assert not oversized, f"{name}: property descriptions over {PROPERTY_DESCRIPTION_LIMIT} characters: {oversized}"


def test_the_report_tools_take_no_input(listed: dict[str, dict]) -> None:
    for name in REPORT_TOOLS:
        assert listed[name]["inputSchema"]["properties"] == {}, name


@pytest.mark.parametrize("name", TOOLS)
def test_the_definitions_use_the_public_words_for_the_hardware(listed: dict[str, dict], name: str) -> None:
    """The unit is the in-circuit debugger or programmer and the target is a board."""
    text = text_of(listed, name)
    assert not claims(text, r"\bST-?Link\b"), text
    assert not re.search(r"\bSTM32\b(?!CubeProgrammer)", text), text


def test_flash_firmware_says_what_it_writes_to_and_through_what(listed: dict[str, dict]) -> None:
    """The first sentence: the board's flash, through the in-circuit debugger or programmer."""
    first = sentences(str(listed[FLASH]["description"]))[0]
    assert claims(first, r"\bflash(?:es)?\b|\bprograms?\b|\bwrites?\b"), first
    assert claims(first, r"\bboard\b"), first
    assert claims(first, r"\bdebugger\b") and claims(first, r"\bprogrammer\b"), first


def test_flash_firmware_names_allow_flash_and_its_refusal(listed: dict[str, dict]) -> None:
    text = text_of(listed, FLASH)
    assert needs_with_refusal(text, "allow_flash"), text


def test_flash_firmware_says_a_debug_session_on_the_probe_makes_it_busy(listed: dict[str, dict]) -> None:
    text = text_of(listed, FLASH)
    assert says_debug_session_makes_flash_busy(text), text


def test_reset_after_flash_names_its_default_and_its_permission(listed: dict[str, dict]) -> None:
    schema = listed[FLASH]["inputSchema"]["properties"]["reset_after_flash"]
    described = property_text(listed[FLASH], "reset_after_flash")
    assert schema.get("default") is False, schema
    assert says_reset_default_is_false(described), described
    assert needs_with_refusal(described, "allow_reset"), described


def test_flash_firmware_says_a_failed_flash_ends_in_a_recovery_reset(listed: dict[str, dict]) -> None:
    text = text_of(listed, FLASH)
    assert says_failed_flash_ends_in_a_recovery_reset(text), text


def test_flash_firmware_names_what_bounds_its_time(listed: dict[str, dict]) -> None:
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


def test_flash_firmware_names_the_image_refusals(listed: dict[str, dict]) -> None:
    text = text_of(listed, FLASH)
    missing = names(text, "artifact_validation_failed", "artifact_not_found", "artifact_too_large", "max_upload_size_mb")
    assert not missing, (missing, text)
    assert one_of(clauses(text), r"\bartifact_too_large\b", r"\bmax_upload_size_mb\b"), text


def test_artifact_upload_says_what_it_stores_and_who_uses_the_id(listed: dict[str, dict]) -> None:
    description = str(listed[UPLOAD]["description"])
    missing = names(description, "artifact_id", FLASH, "debug_start_session")
    assert not missing, (missing, description)
    assert one_of(sentences(description), r"\bartifact_id\b", r"\bflash_firmware\b", r"\bdebug_start_session\b"), description
    assert says_touches_no_board(text_of(listed, UPLOAD)), text_of(listed, UPLOAD)


def test_artifact_upload_names_its_permission_and_its_refusals(listed: dict[str, dict]) -> None:
    text = text_of(listed, UPLOAD)
    assert needs_with_refusal(text, "allow_upload"), text
    missing = names(text, "artifact_validation_failed", "artifact_too_large")
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
    assert says_when_nothing_is_stored(text), text
    assert one_of(sentences(text), r"\bclassify_last_error\b", r"\bfail"), text


def test_get_last_report_says_the_newest_report_can_be_the_recovery(listed: dict[str, dict]) -> None:
    text = text_of(listed, LAST_REPORT)
    assert says_newest_report_may_be_the_recovery(text), text


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


@pytest.mark.parametrize("name", REPORT_TOOLS)
def test_the_report_tools_say_they_need_no_board_or_debugger(listed: dict[str, dict], name: str) -> None:
    text = text_of(listed, name)
    assert says_touches_no_board(text), text


def test_the_annotations_agree_with_what_the_definitions_describe(listed: dict[str, dict]) -> None:
    """A flash erases what was there and repeats its effect; an upload adds a
    content-addressed file; the report tools only read."""
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
    (needs_with_refusal, ("Needs allow_flash, else permission_denied.",), ("Needs no allow_flash; permission_denied comes from elsewhere.", "allow_flash is optional.")),
    (
        says_debug_session_makes_flash_busy,
        ("While a debug session holds the probe it answers resource_busy until debug_stop_session.",),
        ("It stops the debug session first, then flashes; resource_busy is gone after debug_stop_session.", "resource_busy means another server."),
    ),
    (says_reset_default_is_false, ("Default false: the board is not reset.",), ("Defaults to true, so the board is reset.", "False by default.")),
    (
        says_failed_flash_ends_in_a_recovery_reset,
        ("A failed flash is followed by a recovery reset into halt (recovery.auto_recover).",),
        (
            "A failed flash leaves the board untouched.",
            "A failed flash is followed by a recovery reset into halt (recovery.auto_recover); a failed flash leaves the board unchanged.",
            "A failed flash is followed by a recovery reset into halt.",
        ),
    ),
    (says_timeout_bound, ("Bounded by timeout_s (default 60), else timeout.",), ("Bounded by timeout_s, at most 60, else timeout.", "timeout_s defaults to 60.")),
    (says_one_image_input, ("Give image_path or artifact_id, not both.",), ("Both image_path and artifact_id are required.", "Give image_path.")),
    (
        says_content_addressed,
        ("The id is the sha256 of the bytes plus the extension: same bytes, same id.",),
        ("The id is a random uuid; the sha256 plus extension is in the result and the same bytes give the same id.", "The id is the sha256 of the bytes."),
    ),
    (says_whitespace_ignored, ("Padded base64; whitespace is ignored.",), ("Padded base64; whitespace is rejected.", "Padded base64.")),
    (
        says_size_limit,
        ("At most artifacts.max_upload_size_mb after decoding (default 64), else artifact_too_large.",),
        ("At most 64 MB (max_upload_size_mb, default 64), else artifact_too_large.", "max_upload_size_mb applies, default 64."),
    ),
    (says_bare_filename, ("Bare name, no path; its extension ends the artifact_id.",), ("A path to the file; its extension ends the artifact_id.", "Bare name, no path.")),
    (says_touches_no_board, ("Reads files only, no board or debugger needed.",), ("Needs a configured debugger.", "Reads the report files.")),
    (
        says_outer_ok_even_for_a_failed_report,
        ("ok is true even when that report failed; read report.ok and report.error_type.",),
        ("ok is false when that report failed; read report.ok.", "ok is true even when that report failed."),
    ),
    (
        says_newest_report_may_be_the_recovery,
        ("After a failed call it can be the recovery's reset_target or probe_target: check report.tool.",),
        ("It is always the report of the last call you made.", "After a failed call it can be the recovery's reset_target."),
    ),
    (
        says_record_outlives_successes,
        ("It stays until a newer failure, across successes and restarts.", "It stays across successes, so a failed recovery reset names reset_target."),
        ("A success clears it.", "It stays until the next success clears it.", "It stays, but is cleared by a success."),
    ),
    (
        says_some_refusals_record_none,
        ("Some refusals (allow_flash off, bad arguments) record none.",),
        ("Every refusal is recorded, allow_flash off included.", "Some refusals record none."),
    ),
    (says_when_nothing_is_stored, ("No failure yet: report_not_found.",), ("report_not_found means the report file is damaged.",)),
    (
        says_source_may_be_the_recovery_reset,
        ("A failed flash_firmware whose recovery reset failed names reset_target as source_tool.",),
        ("source_tool names the tool that failed.",),
    ),
]


@pytest.mark.parametrize(("check", "accepted", "rejected"), PARAPHRASES, ids=[check.__name__ for check, _, _ in PARAPHRASES])
def test_each_relation_accepts_its_paraphrases_and_rejects_their_inversions(check, accepted: tuple[str, ...], rejected: tuple[str, ...]) -> None:
    arguments = ("allow_flash",) if check is needs_with_refusal else ()
    for text in accepted:
        assert check(text, *arguments), text
    for text in rejected:
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
    """A snake_case word in any of the four definitions is a listed tool, an
    input property of a listed tool, a configuration key, or a string the server
    answers with. Quoted literals are example data and are not read."""
    properties = {key for tool in listed.values() for key in tool["inputSchema"]["properties"]}
    vocabulary = set(listed) | properties | config_vocabulary() | answer_vocabulary()
    assert {"max_upload_size_mb", "allow_upload", "allowed_roots", "source_tool", "likely_causes"} <= vocabulary, "the vocabulary is read whole"
    assert "artifact_unknown" not in vocabulary, "a near miss is still refused"

    unknown = {name: sorted(set(SNAKE_CASE.findall(re.sub(QUOTED, " ", text_of(listed, name)))) - vocabulary) for name in TOOLS}
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


def test_allow_reset_is_needed_only_for_reset_after_flash(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws", permissions={**DEFAULT_TEST_PERMISSIONS, "allow_reset": False})
    try:
        assert flash(service)["ok"] is True
    finally:
        close(service)


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


@pytest.mark.parametrize(
    ("arguments", "error_type"),
    [
        pytest.param({"image_path": "other/app.elf"}, "artifact_validation_failed", id="outside-the-allowed-roots"),
        pytest.param({"image_path": "build/app.txt"}, "artifact_validation_failed", id="extension-not-allowed"),
        pytest.param({"image_path": "build/bad.elf"}, "artifact_validation_failed", id="not-an-elf"),
        pytest.param({"image_path": "build/none.elf"}, "artifact_not_found", id="missing-file"),
        pytest.param({"image_path": "build/big.bin"}, "artifact_too_large", id="over-max_upload_size_mb"),
        pytest.param({"artifact_id": "0" * 64 + ".elf"}, "artifact_not_found", id="unknown-artifact_id"),
        pytest.param({"image_path": IMAGE, "artifact_id": "0" * 64 + ".elf"}, "invalid_argument", id="both-inputs"),
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


def test_a_failed_flash_is_followed_by_a_recovery_reset_into_halt(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws", debugger_executable=FAKE_OPENOCD_ERASE_REFUSED)
    try:
        failed = flash(service)
    finally:
        close(service)
    assert failed["ok"] is False, failed
    assert failed["run"]["aborted"] is True, failed
    assert "reset_halt" in failed["recovery"]["actions"], failed


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"auto_recover": "readonly"}, id="auto_recover-readonly"),
        pytest.param({"permissions": {**DEFAULT_TEST_PERMISSIONS, "allow_reset": False}}, id="allow_reset-off"),
    ],
)
def test_the_recovery_reset_follows_the_policy_and_the_reset_grant(tmp_path: Path, kwargs: dict) -> None:
    service = new_service(tmp_path / "ws", debugger_executable=FAKE_OPENOCD_ERASE_REFUSED, **kwargs)
    try:
        failed = flash(service)
    finally:
        close(service)
    assert failed["ok"] is False, failed
    assert "reset_halt" not in (failed.get("recovery") or {}).get("actions", []), failed


def test_flashing_an_uploaded_artifact_needs_uploads_allowed(tmp_path: Path) -> None:
    service = edited_service(tmp_path / "ws", without_uploads)
    try:
        refused = flash(service, artifact_id="0" * 64 + ".elf")
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


def test_get_last_report_returns_the_flash_report_under_report(tmp_path: Path) -> None:
    service = new_service(tmp_path / "ws")
    try:
        flash(service)
        answer = last_report(service)
        assert answer["ok"] is True, answer
        assert answer["report"]["tool"] == FLASH, answer
        assert answer["report"]["ok"] is True, answer
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
        pytest.param(None, {"image_path": IMAGE, "artifact_id": "0" * 64 + ".elf"}, id="arguments-the-schema-refuses"),
    ],
)
def test_some_refusals_record_no_report(tmp_path: Path, permissions: dict | None, arguments: dict) -> None:
    service = new_service(tmp_path / "ws", permissions=permissions)
    try:
        refused = flash(service, **arguments)
        assert refused["ok"] is False, refused
        assert last_report(service)["error_type"] == "report_not_found"
        assert classify(service)["error_type"] == "report_not_found"
    finally:
        close(service)


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
