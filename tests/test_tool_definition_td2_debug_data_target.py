"""What `tools/list` tells an agent about six debugger tools (#639).

`debug_symbol_value` and `debug_dump_symbol_ihex` read target memory,
`probe_target` and `reset_target` act on the board, and `debugger_info` and
`debugger_probes_list` ask the host what is installed and attached. An agent
reads their definitions, as a host serves them through `tools/list`, to decide
which one fits, what must come first, what it gets back and what each refusal
means.

The first half reads the served definitions and checks their meaning, never
their wording. A keyword alone accepts its own inversion ("no reset" and
"resets", "kept" and "erased", "default run" and "default halt"), so each check
binds the words that carry a claim to one clause or one comma-separated part of
it and rejects the inverted claim outright. Every check is itself held against
paraphrases it must accept and misleading counterexamples it must reject.

The second half holds the behaviour each claim describes, through the fake
debuggers, so a definition that names an outcome the code no longer gives fails
here as surely as one that leaves an outcome out.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import (
    DEFAULT_TEST_PERMISSIONS,
    FAKE_OPENOCD,
    FAKE_OPENOCD_NO_PROBE,
    FAKE_OPENOCD_NO_TARGET,
    FAKE_PYOCD,
    FAKE_STLINK,
    FAKE_STLINK_NO_PROBE,
    FAKE_STLINK_NO_TARGET,
    write_config,
)
from test_debug_sessions import (
    BOOT_COUNTER_SIZE,
    CTC_ARRAY_SIZE,
    debug_service,
    flash_symbol_source,
    pyocd_read_service,
    start_debug_session,
    stlink_dump_service,
)
from test_tool_definition_debug_sessions import (
    CEILING,
    SECONDS,
    definition_text,
    listed_tools,
    property_texts,
    source_literals,
    unknown_identifiers,
)
from test_tool_definition_debug_symbol_info import (
    _says_what_the_sibling_does,
    _segments,
    allow_all_grants_when_true,
    allows_either_policy_key,
    clauses,
    examples,
    names,
    names_symbol_syntax,
    openocd_needs_a_session,
    sentences,
    sessionless_backends_need_this_servers_flashed_elf,
    states_allow_all_default_false,
)

from agentic_hil.backends import openocd as openocd_backend
from agentic_hil.backends import pyocd as pyocd_backend
from agentic_hil.backends import stlink as stlink_backend
from agentic_hil.backends.common import CompletedCommand
from agentic_hil.backends.gdbdebug import DEBUG_SYMBOL_PATTERN, INTEGER_VALUE_WIDTHS
from agentic_hil.config import load_config
from agentic_hil.contracts import validate_tool_arguments
from agentic_hil.tools import AgenticHILToolService

VALUE = "debug_symbol_value"
DUMP = "debug_dump_symbol_ihex"
PROBE = "probe_target"
RESET = "reset_target"
INFO = "debugger_info"
PROBES = "debugger_probes_list"
TOOLS = (VALUE, DUMP, PROBE, RESET, INFO, PROBES)
READS = (VALUE, DUMP)
# The four that work on every backend: none of them may be described as tied to one.
BACKEND_NEUTRAL = (PROBE, RESET, INFO, PROBES)
BACKENDS = ("openocd", "stlink", "pyocd")
BACKEND_NAMES = ("OpenOCD", "pyOCD", "STM32CubeProgrammer")
BACKEND_MODULES = {"openocd": openocd_backend, "stlink": stlink_backend, "pyocd": pyocd_backend}
FAKES = {"openocd": FAKE_OPENOCD, "stlink": FAKE_STLINK, "pyocd": FAKE_PYOCD}
# `min(self.config.debugger.timeout_s, 10)` in each backend's `info`
# (openocd.py:488, stlink.py:276, pyocd.py:245).
VERSION_CHECK_CEILING_S = 10
DESCRIPTION_LIMIT = 400
PROPERTY_LIMIT = 200

NEGATION = r"\b(?:no|not|never|without|nothing|none)\b"
SUCCESS = r"\bsuccess(?:ful)?\b|\bsucceeds?\b"
CONTACT = r"\b(?:contacts?|contacting|connects?|connecting|opens?|touch(?:es)?|talks?\s+to|attach(?:es)?|reach(?:es)?)\b"
OPENOCD_ONLY = r"\bOpenOCD(?:\s+backend)?[\s-]+only\b|\bonly\s+(?:on\s+|with\s+)?(?:the\s+)?OpenOCD\b"

# What makes each refusal happen, as the code decides it. A refusal named with
# no circumstance beside it tells an agent nothing it can act on.
ALLOWLIST = r"allowed_symbols|allow_all_symbols|\bnot\s+(?:allowed|listed)\b"
SESSION = r"\bdebug_start_session\b|\bsession\b"
FLASHED_ELF = r"\bflash_firmware\b|\bflashed\b"
NOT_IN_ELF = r"\b(?:unknown|missing|absent)\b|\bnot\s+(?:found|in|present)\b|\bno\s+such\b"
OUTPUT_RULES = r"\bworkspace\b|allowed_roots|\.hex\b|\.ihex\b|\bextension\b|\.\.|\btraversal\b"
NO_PROBE = r"\bprobes?\b|\badapters?\b|\bprogrammer\b|\bplugged\b|\bUSB\b"
BOARD_SILENT = r"\bboard\b|\btarget\b|\banswer\w*|\brespond\w*|\bpower\w*|\bsilent\b|\bwir\w+"
NOT_INSTALLED = r"\bmissing\b|\bnot\s+installed\b|\bnot\s+found\b|\babsent\b|\buninstalled\b|\bnot\s+on\s+PATH\b"
ALLOW_RESET = r"\ballow_reset\b"
UNREADABLE_ADAPTER = r"\badapters?\b|\binterface_cfg\b"


# --- reading the definitions ---------------------------------------------------


@pytest.fixture(scope="module")
def listed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    return listed_tools(tmp_path_factory.mktemp("listed"))


@pytest.fixture(scope="module")
def known_literals() -> set[str]:
    return source_literals()


def description(listed: dict[str, dict], tool: str) -> str:
    return str(listed[tool]["description"])


def property_text(listed: dict[str, dict], tool: str, name: str) -> str:
    return property_texts(listed[tool]).get(name, "")


def whole(listed: dict[str, dict], tool: str) -> str:
    """Everything a host shows an agent about one tool in words."""
    return definition_text(listed[tool])


def mentions(text: str, term: str) -> bool:
    """`term` as a word of its own, also after a dot (`debug.max_dump_size_bytes`)."""
    return re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", text) is not None


def parts(clause: str) -> list[str]:
    return [part.strip() for part in clause.split(",") if part.strip()]


def first_sentence(text: str) -> str:
    found = sentences(text)
    return found[0] if found else ""


# --- the meaning checks ----------------------------------------------------------


def explains(text: str, error_type: str, circumstance: str) -> bool:
    """The code is named beside what makes it happen.

    Beside means in the same comma-separated part of one clause, or, for a part
    that opens with "else", in the part right before it ("allowed if listed,
    else permission_denied"). A sentence that lists several codes therefore
    cannot lend one code the circumstance of another.
    """
    for clause in clauses(text):
        found = parts(clause)
        for index, part in enumerate(found):
            if not names(part, error_type):
                continue
            context = part
            if index > 0 and re.match(r"(?:else|otherwise)\b", part, re.IGNORECASE):
                context = found[index - 1] + ", " + part
            if re.search(circumstance, context, re.IGNORECASE) and not re.search(SUCCESS, context, re.IGNORECASE):
                return True
    return False


def reads_target_memory(text: str) -> bool:
    first = first_sentence(text)
    return re.search(r"\breads?\b", first, re.IGNORECASE) is not None and re.search(r"\bmemory\b", first, re.IGNORECASE) is not None and not re.search(NEGATION, first, re.IGNORECASE)


def backend_support_is_one_sentence(text: str) -> bool:
    """The description names the backends in exactly one sentence."""
    return len([sentence for sentence in sentences(text) if any(re.search(rf"\b{name}\b", sentence) for name in BACKEND_NAMES)]) == 1


def caps_the_read_size(text: str) -> bool:
    """A symbol over `debug.max_dump_size_bytes` is refused, never one under it."""
    capped = [clause for clause in clauses(text) if mentions(clause, "max_dump_size_bytes") and names(clause, "permission_denied")]
    over = r"\bover\b|\bexceeds?\b|\bexceeding\b|\blarger\b|\bbigger\b|\bmore\s+than\b|\babove\b|\bbeyond\b"
    under = r"\bunder\b|\bbelow\b|\bless\s+than\b|\bsmaller\b|\bwithin\b"
    return any(re.search(over, clause, re.IGNORECASE) for clause in capped) and not any(re.search(under, clause, re.IGNORECASE) for clause in capped)


def names_the_value_readings(text: str) -> bool:
    """`hex` always, the two integer readings only at the widths that have one."""
    if not (names(text, "hex") and names(text, "value_unsigned") and names(text, "value_signed")):
        return False
    readings = [clause for clause in clauses(text) if names(clause, "value_unsigned")]
    widths = r"\b1\b[^;]*\b2\b[^;]*\b4\b[^;]*\b8\b"
    unconditional = r"\balways\b|\bany\s+size\b|\bevery\s+size\b|\ball\s+sizes\b|\bwhatever\s+(?:the\s+)?size\b"
    return any(re.search(widths, clause) for clause in readings) and not any(re.search(unconditional, clause, re.IGNORECASE) for clause in readings)


def names_a_sibling(text: str, sibling: str, meaning: str) -> bool:
    return any(_says_what_the_sibling_does(segment, sibling, meaning) for segment in _segments(text))


def writes_intel_hex_to_output_path(text: str) -> bool:
    first = first_sentence(text)
    return (
        re.search(r"\bIntel\s+HEX\b", first) is not None
        and names(first, "output_path")
        and re.search(r"\b(?:writes?|saves?|stores?)\b", first, re.IGNORECASE) is not None
    )


def names_the_dump_result(text: str) -> bool:
    return any(
        re.search(r"\breturns?\b", clause, re.IGNORECASE) and names(clause, "output") and names(clause, "address") and names(clause, "size_bytes")
        for clause in clauses(text)
    )


def keeps_the_output_in_the_workspace(text: str) -> bool:
    inside = re.search(r"\b(?:in|inside|within|under)\s+(?:the\s+)?workspace\b|\bworkspace(?:-relative)?\s+path\b|\brelative\s+to\s+the\s+workspace\b", text, re.IGNORECASE)
    outside_offered = any(
        re.search(r"\boutside\b[^,;]*\bworkspace\b", part, re.IGNORECASE) and not re.search(r"\b(?:not|never|no)\b|output_validation_failed|\brefused\b|\brejected\b", part, re.IGNORECASE)
        for clause in clauses(text)
        for part in parts(clause)
    )
    return inside is not None and not outside_offered


def names_both_hex_extensions(text: str) -> bool:
    return mentions(text, ".hex") and mentions(text, ".ihex")


def output_examples_are_workspace_hex_paths(text: str) -> bool:
    """Every example is relative, stays inside, and ends .hex or .ihex."""
    return all(
        re.search(r"\.(?:hex|ihex)$", example, re.IGNORECASE) and ".." not in example and not re.match(r"[/\\]|[A-Za-z]:", example)
        for example in examples(text)
    )


def says_it_contacts_no_board(text: str) -> bool:
    """A denial of contact with the board, and no claim of it, for this tool.

    A part that names another tool is about that tool ("probe_target connects
    to the board"), so it is left out of both halves.
    """
    others = ("probe_target", "reset_target", "flash_firmware", "debug_start_session")
    contact = [
        segment
        for segment in _segments(text)
        if not any(names(segment, other) for other in others) and re.search(CONTACT, segment, re.IGNORECASE) and re.search(r"\b(?:board|target|core)\b", segment, re.IGNORECASE)
    ]
    return any(re.search(NEGATION, segment, re.IGNORECASE) for segment in contact) and not any(not re.search(NEGATION, segment, re.IGNORECASE) for segment in contact)


def checks_the_backend_version(text: str) -> bool:
    first = first_sentence(text)
    return re.search(r"\bversion\b", first, re.IGNORECASE) is not None and re.search(r"\b(?:installed|available|present)\b", first, re.IGNORECASE) is not None


def bounded_at_ten_seconds(text: str) -> bool:
    ceiling = re.compile(CEILING.pattern + r"|\bwithin\b|\bup\s+to\b", re.IGNORECASE)
    return any(SECONDS.search(clause) and ceiling.search(clause) and re.search(rf"\b{VERSION_CHECK_CEILING_S}\b", clause) for clause in clauses(text))


def names_the_info_result(text: str) -> bool:
    return any(all(names(clause, field) for field in ("backend", "executable", "version", "config_status")) for clause in clauses(text))


def explains_the_unbound_refusal(text: str) -> bool:
    """`not_supported` unless exactly one debugger is configured, never "when one is"."""
    refused = [clause for clause in clauses(text) if names(clause, "not_supported")]
    unbound = r"\bunless\b[^;]*\bone\b|\bnot\s+exactly\s+one\b|\bmore\s+than\s+one\b|\bno\s+debugger|\bnone\b|\bseveral\b|\bmultiple\b|\btwo\s+or\s+more\b"
    inverted = r"\b(?:if|when|while|with)\s+(?:exactly\s+|only\s+)?one\s+debugger\b"
    return any(re.search(unbound, clause, re.IGNORECASE) for clause in refused) and not any(re.search(inverted, clause, re.IGNORECASE) for clause in refused)


def lists_probe_ids(text: str) -> bool:
    first = first_sentence(text)
    return re.search(r"\blists?\b", first, re.IGNORECASE) is not None and re.search(r"\bprobe\s+ids?\b|\bprobe_ids?\b|\bprobes\b", first, re.IGNORECASE) is not None


def says_where_the_probe_id_goes(text: str) -> bool:
    return any(names(clause, "probe_id") and re.search(r"\bentry\b|\bconfig(?:uration)?\b|\bdebuggers\b|\bselects?\b", clause, re.IGNORECASE) for clause in clauses(text))


COMPLETE_FALSE = r"\bcomplete\b\W{0,3}(?:is\s+|=\s*|:\s*)?false\b"
COMPLETE_TRUE = r"\bcomplete\b\W{0,3}(?:is\s+|=\s*|:\s*)?true\b"


def openocd_listing_reads_the_usb_inventory(text: str) -> bool:
    """OpenOCD lists from the host's USB serial inventory, never as a finished count."""
    openocd = [clause for clause in clauses(text) if re.search(r"\bOpenOCD\b", clause)]
    incomplete = any(re.search(COMPLETE_FALSE, clause, re.IGNORECASE) and re.search(r"\bUSB\b|\binventory\b", clause, re.IGNORECASE) for clause in openocd)
    complete = any(re.search(COMPLETE_TRUE, clause, re.IGNORECASE) for clause in openocd)
    cannot = any(re.search(r"\b(?:cannot|can't|never|does\s+not|doesn't)\b[^,;]{0,20}\b(?:enumerate|list)", clause, re.IGNORECASE) for clause in openocd)
    return incomplete and not complete and not cannot


def openocd_refuses_an_adapter_it_cannot_read(text: str) -> bool:
    return any(re.search(r"\bOpenOCD\b", clause) and names(clause, "not_supported") and re.search(UNREADABLE_ADAPTER, clause, re.IGNORECASE) for clause in clauses(text))


def connects_and_confirms_the_board(text: str) -> bool:
    first = first_sentence(text)
    return (
        re.search(r"\b(?:connects?|attach(?:es)?|reach(?:es)?|checks?|confirms?|verif(?:y|ies))\b", first, re.IGNORECASE) is not None
        and re.search(r"\b(?:board|target)\b", first, re.IGNORECASE) is not None
    )


def names_target_detected_true(text: str) -> bool:
    return re.search(r"\btarget_detected\b\W{0,3}(?:is\s+|=\s*|:\s*)?true\b", text, re.IGNORECASE) is not None


RESET_WORD = r"\breset(?:s|ting)?\b"


def says_it_does_not_reset(text: str) -> bool:
    """A denial of a reset by this tool, and no reset claimed for it."""
    own = [segment for segment in _segments(text) if not names(segment, "reset_target") and re.search(RESET_WORD, segment, re.IGNORECASE)]
    return any(re.search(NEGATION, segment, re.IGNORECASE) for segment in own) and not any(not re.search(NEGATION, segment, re.IGNORECASE) for segment in own)


def makes_no_halt_claim(text: str) -> bool:
    """Whether a connect leaves the core halted depends on the probe and the part,
    and nothing here can observe it, so the definition claims neither way."""
    return re.search(r"\bhalt", text, re.IGNORECASE) is None


def resets_the_board(text: str) -> bool:
    first = first_sentence(text)
    return re.search(r"\breset", first, re.IGNORECASE) is not None and re.search(r"\b(?:board|target)\b", first, re.IGNORECASE) is not None


def keeps_flash(text: str) -> bool:
    """Flash is said to be kept, and never said to be erased or written."""
    flash = [segment for segment in _segments(text) if re.search(r"\bflash\b", segment, re.IGNORECASE)]
    kept = r"\b(?:keeps?|kept|untouched|unchanged|preserved|stays|remains|retained|intact)\b|\bnot\s+(?:erased|written|programmed|touched|changed)\b"
    changed = r"\b(?:erases?|erased|erasing|programs?|programmed|writes?|written|wipes?|wiped|clears?|cleared)\b"
    return any(re.search(kept, segment, re.IGNORECASE) for segment in flash) and not any(
        re.search(changed, segment, re.IGNORECASE) and not re.search(NEGATION, segment, re.IGNORECASE) for segment in flash
    )


def points_to_probe_target_for_a_check(text: str) -> bool:
    """probe_target is the call that checks the board without a reset.

    The "without a reset" is this tool's contrast, not a denial of the sibling,
    so it is taken out before the sibling check looks for one.
    """
    plain = re.sub(r"\b(?:without|with\s+no|no)\s+(?:a\s+)?reset(?:ting)?\b", "", text, flags=re.IGNORECASE)
    return names_a_sibling(plain, "probe_target", r"\bchecks?\b|\bconfirms?\b|\bconnects?\b|\breach(?:es)?\b|\bprobes?\b")


def names_run_as_the_default(text: str) -> bool:
    run = r"\bdefaults?\b\W{0,3}(?:is\s+|to\s+)?'?run'?|'?\brun'?\s*\(\s*default\b|'?\brun'?\s+is\s+the\s+default\b|'?\brun'?\s+by\s+default\b"
    other = r"\bdefaults?\b\W{0,3}(?:is\s+|to\s+)?'?(?:halt|init)\b|'?\b(?:halt|init)'?\s*\(\s*default\b|'?\b(?:halt|init)'?\s+(?:is\s+the\s+default|by\s+default)\b"
    return re.search(run, text, re.IGNORECASE) is not None and re.search(other, text, re.IGNORECASE) is None


def init_is_openocd_only(text: str) -> bool:
    """`init` runs on OpenOCD alone, and the other backends answer not_supported."""
    init = [clause for clause in clauses(text) if re.search(r"\binit\b", clause)]
    only = any(re.search(OPENOCD_ONLY, clause, re.IGNORECASE) for clause in init)
    refused = any(names(clause, "not_supported") and re.search(r"\bother\b|\bothers\b|\belse\b|\botherwise\b|\bpyOCD\b|\bSTM32CubeProgrammer\b|\belsewhere\b|\bonly\b", clause) for clause in init)
    blamed = re.search(
        r"\bOpenOCD\b(?!(?:\s+backend)?[\s-]+only\b)[^.;,:]{0,30}\b(?:answers?|returns?|gives?|refuses?)\s+(?:with\s+)?not_supported|not_supported\s+(?:on|with|under|from)\s+OpenOCD\b",
        text,
        re.IGNORECASE,
    )
    return only and refused and blamed is None


def allow_probe_is_qualified(text: str) -> bool:
    """`allow_probe` is a version 1 key: any clause naming it says so."""
    return all(re.search(r"\bversion\s*1\b|\blegacy\b|\bv1\b", clause, re.IGNORECASE) for clause in clauses(text) if names(clause, "allow_probe"))


def uses_the_audience_words(text: str) -> bool:
    """The probe is the in-circuit debugger or programmer, the target a board."""
    return not re.search(r"\bST-?LINK\b", text, re.IGNORECASE) and not re.search(r"\bSTM32(?!CubeProgrammer\b)", text) and not re.search(r"\bNucleo\b", text, re.IGNORECASE)


def not_tied_to_one_backend(text: str) -> bool:
    return not any(re.search(OPENOCD_ONLY, clause, re.IGNORECASE) and not re.search(r"\binit\b", clause) for clause in clauses(text))


# --- the checks accept paraphrases and reject inversions --------------------------


@pytest.mark.parametrize(
    ("check", "text", "accepted"),
    [
        (reads_target_memory, "Read an allowed symbol's bytes from target memory.", True),
        (reads_target_memory, "Reads the memory a symbol occupies on the board.", True),
        (reads_target_memory, "Find a symbol's address. Reads no memory.", False),
        (reads_target_memory, "Reads no target memory.", False),
        (backend_support_is_one_sentence, "Read memory. OpenOCD needs debug_start_session; pyOCD and STM32CubeProgrammer, the ELF last flashed.", True),
        (backend_support_is_one_sentence, "Read memory. OpenOCD needs debug_start_session. pyOCD and STM32CubeProgrammer use the ELF last flashed.", False),
        (backend_support_is_one_sentence, "Read memory.", False),
        (caps_the_read_size, "Over debug.max_dump_size_bytes: permission_denied.", True),
        (caps_the_read_size, "A symbol larger than debug.max_dump_size_bytes answers permission_denied.", True),
        (caps_the_read_size, "Under debug.max_dump_size_bytes: permission_denied.", False),
        (caps_the_read_size, "debug.max_dump_size_bytes caps the read.", False),
        (names_the_value_readings, "Returns hex, plus value_unsigned and value_signed if size_bytes is 1, 2, 4 or 8.", True),
        (names_the_value_readings, "hex always; value_unsigned and value_signed for 1, 2, 4 or 8 bytes.", True),
        (names_the_value_readings, "Returns hex, value_unsigned and value_signed for any size.", False),
        (names_the_value_readings, "Returns hex, value_unsigned and value_signed.", False),
        (names_the_value_readings, "Returns value_unsigned and value_signed if size_bytes is 1, 2, 4 or 8.", False),
        (writes_intel_hex_to_output_path, "Read a symbol's bytes from target memory and write them as Intel HEX to output_path.", True),
        (writes_intel_hex_to_output_path, "Saves the bytes to output_path as Intel HEX.", True),
        (writes_intel_hex_to_output_path, "Read a symbol from target memory and write Intel HEX.", False),
        (names_the_dump_result, "Writes Intel HEX; returns address, size_bytes and output.", True),
        (names_the_dump_result, "Writes Intel HEX to output_path.", False),
        (keeps_the_output_in_the_workspace, "Workspace path ending .hex or .ihex.", True),
        (keeps_the_output_in_the_workspace, "A path inside the workspace; outside the workspace: output_validation_failed.", True),
        (keeps_the_output_in_the_workspace, "Workspace path, or any path outside the workspace.", False),
        (keeps_the_output_in_the_workspace, "Path of the .hex file.", False),
        (names_both_hex_extensions, "Path ending .hex or .ihex.", True),
        (names_both_hex_extensions, "Path ending .hex.", False),
        (output_examples_are_workspace_hex_paths, "Workspace path, e.g. build/counter.hex.", True),
        (output_examples_are_workspace_hex_paths, "Workspace path, e.g. build/counter.ihex or build/a.hex.", True),
        (output_examples_are_workspace_hex_paths, "Workspace path, e.g. build/counter.bin.", False),
        (output_examples_are_workspace_hex_paths, "Workspace path, e.g. ../counter.hex.", False),
        (output_examples_are_workspace_hex_paths, "Workspace path, e.g. /tmp/counter.hex.", False),
        (says_it_contacts_no_board, "Runs its version command only; contacts no probe or board.", True),
        (says_it_contacts_no_board, "Never connects to the board.", True),
        (says_it_contacts_no_board, "Connects to the board to read the version.", False),
        (says_it_contacts_no_board, "Contacts the board; no probe is opened.", False),
        (says_it_contacts_no_board, "Reads the version; probe_target connects to the board.", False),
        (checks_the_backend_version, "Check the configured debugger backend is installed by running its version command.", True),
        (checks_the_backend_version, "Check whether the configured debugger backend is available.", False),
        (bounded_at_ten_seconds, "Runs its version command only, at most 10 s.", True),
        (bounded_at_ten_seconds, "The version check is capped at 10 seconds.", True),
        (bounded_at_ten_seconds, "The version check takes at least 10 s.", False),
        (bounded_at_ten_seconds, "The version check takes at most 60 s.", False),
        (bounded_at_ten_seconds, "The version check takes 10 s.", False),
        (names_the_info_result, "Returns backend, executable, version and config_status.", True),
        (names_the_info_result, "Returns backend, executable and version.", False),
        (explains_the_unbound_refusal, "not_supported unless exactly one debugger is configured.", True),
        (explains_the_unbound_refusal, "With no debugger or more than one configured: not_supported.", True),
        (explains_the_unbound_refusal, "not_supported when exactly one debugger is configured.", False),
        (explains_the_unbound_refusal, "Failures: not_supported, timeout.", False),
        (lists_probe_ids, "List the probe ids of the in-circuit debuggers or programmers attached to this host.", True),
        (lists_probe_ids, "Lists the probes attached.", True),
        (lists_probe_ids, "Check whether the backend is available.", False),
        (says_where_the_probe_id_goes, "Returns probes, each with a probe_id to set as the debugger entry's probe_id.", True),
        (says_where_the_probe_id_goes, "Returns probes, each with probe_id.", False),
        (openocd_listing_reads_the_usb_inventory, "OpenOCD reads the USB serial inventory and answers complete false.", True),
        (openocd_listing_reads_the_usb_inventory, "On OpenOCD the listing comes from the host's USB inventory, complete: false.", True),
        (openocd_listing_reads_the_usb_inventory, "OpenOCD reads the USB serial inventory and answers complete true.", False),
        (openocd_listing_reads_the_usb_inventory, "OpenOCD cannot enumerate probes; it reads the USB inventory, complete false.", False),
        (openocd_listing_reads_the_usb_inventory, "pyOCD reads the USB serial inventory and answers complete false.", False),
        (openocd_refuses_an_adapter_it_cannot_read, "OpenOCD reads the USB serial inventory (adapters it cannot read there: not_supported).", True),
        (openocd_refuses_an_adapter_it_cannot_read, "pyOCD answers not_supported for adapters it cannot read.", False),
        (openocd_refuses_an_adapter_it_cannot_read, "OpenOCD answers not_supported.", False),
        (connects_and_confirms_the_board, "Connect the in-circuit debugger or programmer to the board and confirm it answers.", True),
        (connects_and_confirms_the_board, "Checks that the embedded target answers.", True),
        (connects_and_confirms_the_board, "Probe the configured embedded target through the configured debugger.", False),
        (names_target_detected_true, "Confirm it answers (target_detected true).", True),
        (names_target_detected_true, "Returns target_detected: true.", True),
        (names_target_detected_true, "Returns target_detected.", False),
        (names_target_detected_true, "Returns target_detected false.", False),
        (says_it_does_not_reset, "Connects with no reset and no flash; reset_target resets.", True),
        (says_it_does_not_reset, "Never resets the board.", True),
        (says_it_does_not_reset, "Connects and resets the board.", False),
        (says_it_does_not_reset, "Connects without resetting, then resets the core.", False),
        (says_it_does_not_reset, "Connects; reset_target resets.", False),
        (makes_no_halt_claim, "Connects with no reset.", True),
        (makes_no_halt_claim, "Connects and halts the core.", False),
        (makes_no_halt_claim, "Connects and never halts the core.", False),
        (resets_the_board, "Reset the board through the in-circuit debugger or programmer.", True),
        (resets_the_board, "Resets the embedded target.", True),
        (resets_the_board, "Check the board answers.", False),
        (keeps_flash, "The firmware restarts, flash is kept.", True),
        (keeps_flash, "Flash is not erased.", True),
        (keeps_flash, "The firmware restarts and flash is erased.", False),
        (keeps_flash, "Flash is kept; the reset writes flash again.", False),
        (keeps_flash, "The firmware restarts.", False),
        (points_to_probe_target_for_a_check, "To check the board without a reset use probe_target.", True),
        (points_to_probe_target_for_a_check, "probe_target checks the board with no reset.", True),
        (points_to_probe_target_for_a_check, "Do not use probe_target to check the board.", False),
        (points_to_probe_target_for_a_check, "To check the board, read the summary.", False),
        (names_run_as_the_default, "Default 'run' (core executes); 'halt' stops the core at reset.", True),
        (names_run_as_the_default, "'run' (default) executes, 'halt' stops the core.", True),
        (names_run_as_the_default, "'run' is the default.", True),
        (names_run_as_the_default, "Default 'halt'; 'run' executes.", False),
        (names_run_as_the_default, "'run' executes, 'halt' stops the core.", False),
        (init_is_openocd_only, "'init' also runs the reset-init script, OpenOCD-only, else not_supported.", True),
        (init_is_openocd_only, "'init' runs the reset-init script and is OpenOCD-only: other backends answer not_supported.", True),
        (init_is_openocd_only, "'init' runs the reset-init script; OpenOCD answers not_supported.", False),
        (init_is_openocd_only, "'init' runs the reset-init script, OpenOCD-only.", False),
        (allow_probe_is_qualified, "Version 1 configs need allow_probe.", True),
        (allow_probe_is_qualified, "Connects to the board.", True),
        (allow_probe_is_qualified, "Needs allow_probe (else permission_denied).", False),
        (uses_the_audience_words, "The in-circuit debugger or programmer; STM32CubeProgrammer answers.", True),
        (uses_the_audience_words, "Connect the ST-Link to the board.", False),
        (uses_the_audience_words, "Connect the STLINK to the board.", False),
        (uses_the_audience_words, "Connect to the STM32 board.", False),
        (uses_the_audience_words, "Connect to the Nucleo.", False),
        (not_tied_to_one_backend, "Reset the board; 'init' is OpenOCD-only.", True),
        (not_tied_to_one_backend, "Reset the board. OpenOCD backend only.", False),
        (not_tied_to_one_backend, "Lists probes, only on OpenOCD.", False),
    ],
)
def test_each_check_accepts_a_paraphrase_and_rejects_an_inversion(check: Callable[[str], bool], text: str, accepted: bool) -> None:
    assert bool(check(text)) is accepted, (check.__name__, text)


@pytest.mark.parametrize(
    ("text", "error_type", "circumstance", "accepted"),
    [
        ("Allowed if in debug.allowed_symbols or debug.allow_all_symbols is true, else permission_denied.", "permission_denied", ALLOWLIST, True),
        ("Over debug.max_dump_size_bytes: permission_denied.", "permission_denied", ALLOWLIST, False),
        ("OpenOCD needs debug_start_session (else session_not_active).", "session_not_active", SESSION, True),
        ("Failures: session_not_active, symbol_not_found.", "session_not_active", SESSION, False),
        ("pyOCD and STM32CubeProgrammer, the ELF last flashed via flash_firmware (else symbol_source_not_available).", "symbol_source_not_available", FLASHED_ELF, True),
        ("Not in the ELF: symbol_not_found.", "symbol_not_found", NOT_IN_ELF, True),
        ("Failures: symbol_not_found, permission_denied.", "symbol_not_found", NOT_IN_ELF, False),
        ("Under artifacts.allowed_roots (else output_validation_failed).", "output_validation_failed", OUTPUT_RULES, True),
        ("Failures: output_validation_failed.", "output_validation_failed", OUTPUT_RULES, False),
        ("Refused with resource_busy while a debug session is open.", "resource_busy", SESSION, True),
        ("Failures: resource_busy, target_not_detected (no debug session).", "resource_busy", SESSION, False),
        ("Failures: adapter_not_found (no probe), target_not_detected (board not answering).", "adapter_not_found", NO_PROBE, True),
        ("Failures: adapter_not_found (no probe), target_not_detected (board not answering).", "target_not_detected", BOARD_SILENT, True),
        ("Failures: adapter_not_found (board not answering), target_not_detected (no probe).", "adapter_not_found", NO_PROBE, False),
        ("Failures: adapter_not_found (board not answering), target_not_detected (no probe).", "target_not_detected", BOARD_SILENT, False),
        ("Failures: debugger_not_found (backend not installed), timeout.", "debugger_not_found", NOT_INSTALLED, True),
        ("Failures: debugger_not_found, timeout.", "debugger_not_found", NOT_INSTALLED, False),
        ("Needs allow_reset (else permission_denied).", "permission_denied", ALLOW_RESET, True),
        ("A success even when allow_reset is off: permission_denied.", "permission_denied", ALLOW_RESET, False),
    ],
)
def test_the_refusal_check_wants_the_circumstance_beside_the_code(text: str, error_type: str, circumstance: str, accepted: bool) -> None:
    assert explains(text, error_type, circumstance) is accepted, (error_type, text)


# --- all six definitions -----------------------------------------------------------


@pytest.mark.parametrize("tool", TOOLS)
def test_every_input_property_carries_its_own_description(listed: dict[str, dict], tool: str) -> None:
    for name, text in property_texts(listed[tool]).items():
        assert text.strip(), (tool, name)


@pytest.mark.parametrize("tool", TOOLS)
def test_each_description_fits_the_listing_budget(listed: dict[str, dict], tool: str) -> None:
    assert len(description(listed, tool)) <= DESCRIPTION_LIMIT, (tool, len(description(listed, tool)))
    for name, text in property_texts(listed[tool]).items():
        assert len(text) <= PROPERTY_LIMIT, (tool, name, len(text))


@pytest.mark.parametrize("tool", TOOLS)
def test_every_identifier_a_definition_names_exists(listed: dict[str, dict], known_literals: set[str], tool: str) -> None:
    """Each snake_case word is a listed tool, an argument, a string the source
    produces or reads, or a symbol an example offers."""
    text = whole(listed, tool)
    offered = {word for example in examples(text) for word in re.findall(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", example)}

    assert unknown_identifiers(text, listed, listed[tool], known_literals | offered) == []


@pytest.mark.parametrize("tool", TOOLS)
def test_the_definitions_name_the_probe_and_the_board_the_project_way(listed: dict[str, dict], tool: str) -> None:
    text = whole(listed, tool)
    assert uses_the_audience_words(text), text
    assert allow_probe_is_qualified(text), text


@pytest.mark.parametrize("tool", BACKEND_NEUTRAL)
def test_the_tools_that_run_on_every_backend_are_not_described_as_tied_to_one(listed: dict[str, dict], tool: str) -> None:
    assert not_tied_to_one_backend(description(listed, tool)), description(listed, tool)


def test_the_annotations_keep_what_the_code_does(listed: dict[str, dict]) -> None:
    def hints(tool: str) -> tuple:
        annotations = listed[tool]["annotations"]
        return (annotations.get("readOnlyHint"), annotations.get("destructiveHint"), annotations.get("idempotentHint"), annotations.get("openWorldHint"))

    assert hints(INFO) == (True, None, None, False)
    assert hints(PROBES) == (True, None, None, False)
    assert hints(PROBE) == (False, False, True, False)
    assert hints(RESET) == (False, False, False, False)
    assert hints(VALUE) == (True, None, None, False)
    assert hints(DUMP) == (False, True, False, False)


# --- the two symbol reads: definitions --------------------------------------------


@pytest.mark.parametrize("tool", READS)
def test_each_read_says_it_reads_target_memory(listed: dict[str, dict], tool: str) -> None:
    assert reads_target_memory(description(listed, tool)), description(listed, tool)


@pytest.mark.parametrize("tool", READS)
def test_each_symbol_description_names_the_syntax_and_gives_only_valid_examples(listed: dict[str, dict], tool: str) -> None:
    text = property_text(listed, tool, "symbol")
    assert names_symbol_syntax(text), text
    offered = examples(text)
    assert offered, text
    others = {"output_path": "build/symbol.hex"} if tool == DUMP else {}
    for example in offered:
        assert validate_tool_arguments(tool, {"symbol": example, **others}) is None, (example, text)
        assert DEBUG_SYMBOL_PATTERN.match(example), (example, text)


@pytest.mark.parametrize("tool", READS)
def test_each_read_states_the_symbol_policy_and_its_refusals(listed: dict[str, dict], tool: str) -> None:
    text = whole(listed, tool)
    assert allows_either_policy_key(text), text
    assert allow_all_grants_when_true(text), text
    assert states_allow_all_default_false(text), text
    assert explains(text, "permission_denied", ALLOWLIST), text
    assert explains(text, "symbol_not_found", NOT_IN_ELF), text


@pytest.mark.parametrize("tool", READS)
def test_each_read_ties_each_prerequisite_to_its_backend_in_one_sentence(listed: dict[str, dict], tool: str) -> None:
    """OpenOCD reads through the session; pyOCD and STM32CubeProgrammer from the ELF
    this server flashed. Said once, so a change to that support edits one phrase."""
    text = description(listed, tool)
    assert backend_support_is_one_sentence(text), text
    for name, prop in property_texts(listed[tool]).items():
        assert not any(re.search(rf"\b{backend}\b", prop, re.IGNORECASE) for backend in BACKEND_NAMES), (name, prop)
    assert openocd_needs_a_session(text), text
    assert sessionless_backends_need_this_servers_flashed_elf(text), text
    assert explains(text, "session_not_active", SESSION), text
    assert explains(text, "symbol_source_not_available", FLASHED_ELF), text


@pytest.mark.parametrize("tool", READS)
def test_each_read_states_the_size_cap(listed: dict[str, dict], tool: str) -> None:
    assert caps_the_read_size(whole(listed, tool)), whole(listed, tool)


def test_the_value_read_names_its_readings_and_when_the_integers_come(listed: dict[str, dict]) -> None:
    assert names_the_value_readings(description(listed, VALUE)), description(listed, VALUE)


def test_the_value_read_tells_itself_apart_from_its_siblings(listed: dict[str, dict]) -> None:
    text = description(listed, VALUE)
    assert names_a_sibling(text, "debug_symbol_info", r"\baddress(?:es)?\b|\bsize\b|\bwhere\b|\blocation\b"), text
    assert names_a_sibling(text, DUMP, r"\bfiles?\b|\bIntel\s+HEX\b|\.hex\b"), text


def test_the_dump_says_what_it_writes_where_and_what_it_returns(listed: dict[str, dict]) -> None:
    text = description(listed, DUMP)
    assert writes_intel_hex_to_output_path(text), text
    assert names_the_dump_result(text), text
    assert names_a_sibling(text, VALUE, r"\bbytes?\b|\bvalues?\b|\binline\b|\bintegers?\b"), text


def test_the_output_path_description_names_every_rule_the_validator_applies(listed: dict[str, dict]) -> None:
    """artifacts.py validate_output_path: inside the workspace, under the allowed
    roots, ending .hex or .ihex, each refused with output_validation_failed."""
    text = property_text(listed, DUMP, "output_path")
    assert keeps_the_output_in_the_workspace(text), text
    assert mentions(text, "allowed_roots"), text
    assert names_both_hex_extensions(text), text
    assert explains(text, "output_validation_failed", OUTPUT_RULES), text
    assert output_examples_are_workspace_hex_paths(text), text


# --- probe_target: definition ---------------------------------------------------


def test_probe_says_it_connects_and_confirms_the_board_without_a_reset(listed: dict[str, dict]) -> None:
    text = description(listed, PROBE)
    assert connects_and_confirms_the_board(text), text
    assert names_target_detected_true(text), text
    assert says_it_does_not_reset(text), text
    assert names_a_sibling(text, RESET, RESET_WORD + r"|\brestart"), text
    assert makes_no_halt_claim(whole(listed, PROBE)), whole(listed, PROBE)


def test_probe_explains_each_failure_an_agent_acts_on(listed: dict[str, dict]) -> None:
    text = whole(listed, PROBE)
    assert explains(text, "resource_busy", SESSION), text
    assert explains(text, "adapter_not_found", NO_PROBE), text
    assert explains(text, "target_not_detected", BOARD_SILENT), text


# --- reset_target: definition ----------------------------------------------------


def test_reset_says_it_resets_the_board_and_keeps_flash(listed: dict[str, dict]) -> None:
    text = description(listed, RESET)
    assert resets_the_board(text), text
    assert keeps_flash(text), text
    assert points_to_probe_target_for_a_check(text), text


def test_reset_explains_its_prerequisites(listed: dict[str, dict]) -> None:
    text = whole(listed, RESET)
    assert explains(text, "permission_denied", ALLOW_RESET), text
    assert explains(text, "resource_busy", SESSION), text


def test_reset_mode_names_its_default_and_where_init_runs(listed: dict[str, dict]) -> None:
    text = property_text(listed, RESET, "mode")
    assert listed[RESET]["inputSchema"]["properties"]["mode"]["default"] == "run"
    assert names_run_as_the_default(text), text
    assert init_is_openocd_only(text), text


# --- debugger_info: definition ---------------------------------------------------


def test_info_says_it_runs_only_a_bounded_version_check(listed: dict[str, dict]) -> None:
    text = description(listed, INFO)
    assert checks_the_backend_version(text), text
    assert says_it_contacts_no_board(text), text
    assert bounded_at_ten_seconds(text), text


def test_info_names_its_result_its_failures_and_what_comes_next(listed: dict[str, dict]) -> None:
    text = description(listed, INFO)
    assert names_the_info_result(text), text
    assert explains(text, "debugger_not_found", NOT_INSTALLED), text
    assert names(text, "timeout"), text
    assert explains_the_unbound_refusal(text), text
    assert names_a_sibling(text, PROBES, r"\bprobe\s+ids?\b|\bprobes?\b|\blists?\b|\bprobe_id\b"), text
    assert names_a_sibling(text, PROBE, r"\bboard\b|\btarget\b|\bconnects?\b|\breach(?:es)?\b"), text


# --- debugger_probes_list: definition --------------------------------------------


def test_probes_says_what_it_lists_and_that_no_board_is_contacted(listed: dict[str, dict]) -> None:
    text = description(listed, PROBES)
    assert lists_probe_ids(text), text
    assert says_it_contacts_no_board(text), text
    assert names(text, "probes") and names(text, "probe_id"), text
    assert says_where_the_probe_id_goes(text), text


def test_probes_says_how_openocd_lists_and_what_it_refuses(listed: dict[str, dict]) -> None:
    text = description(listed, PROBES)
    assert openocd_listing_reads_the_usb_inventory(text), text
    assert openocd_refuses_an_adapter_it_cannot_read(text), text
    assert names(text, "probe_discovery_failed"), text


# ---------------------------------------------------------------------------
# The behaviour the definitions describe, through the fake debuggers.


def bench(tmp_path: Path, backend: str, **config_kwargs) -> AgenticHILToolService:
    executable = config_kwargs.pop("debugger_executable", FAKES[backend])
    target_type = "stm32f446re" if backend == "pyocd" else None
    return AgenticHILToolService(load_config(str(write_config(tmp_path, debugger_type=backend, debugger_executable=executable, target_type=target_type, **config_kwargs))))


def call(service: AgenticHILToolService, tool: str, arguments: dict | None = None) -> dict:
    try:
        return service.call(tool, arguments or {})
    finally:
        service.close()


def recorded_spawns(monkeypatch: pytest.MonkeyPatch, backend: str) -> list[tuple[list[str], float]]:
    """Every command the backend spawns, with the timeout it asks for."""
    module = BACKEND_MODULES[backend]
    original = module.spawn_command
    seen: list[tuple[list[str], float]] = []

    def wrapper(command: list[str], cwd: str, timeout_seconds: float) -> CompletedCommand:
        seen.append(([str(part) for part in command], timeout_seconds))
        return original(command, cwd, timeout_seconds)

    monkeypatch.setattr(module, "spawn_command", wrapper)
    return seen


def openocd_commands(spawns: list[tuple[list[str], float]]) -> list[str]:
    """The `-c` command lines of every OpenOCD spawn. Matched on these alone,
    because the paths around them carry the test's own name."""
    return [command[index + 1] for command, _ in spawns for index, part in enumerate(command[:-1]) if part == "-c"]


def pyocd_commands(spawns: list[tuple[list[str], float]]) -> list[str]:
    return [command[index + 1] for command, _ in spawns for index, part in enumerate(command[:-1]) if part == "--command"]


TWO_DEBUGGERS = 'debuggers:\n  probe_b:\n    type: openocd\n    probe_id: "PROBE-B"\n'
ARGUMENTS = {VALUE: {"symbol": "boot_counter"}, DUMP: {"symbol": "boot_counter", "output_path": "build/unbound.hex"}}


@pytest.mark.parametrize("tool", TOOLS)
def test_every_tool_answers_not_supported_unless_exactly_one_debugger_is_configured(tmp_path: Path, tool: str) -> None:
    service = AgenticHILToolService(load_config(str(write_config(tmp_path, probe_id="PROBE-A", debuggers_yaml=TWO_DEBUGGERS))))
    answer = call(service, tool, ARGUMENTS.get(tool))

    assert answer["ok"] is False, answer
    assert answer["error_type"] == "not_supported", answer


# --- debugger_info ------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_info_runs_only_the_version_command_within_ten_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    spawns = recorded_spawns(monkeypatch, backend)
    answer = call(bench(tmp_path, backend, timeout_s=30), INFO)

    assert answer["ok"] is True, answer
    for field in ("backend", "executable", "version", "config_status"):
        assert field in answer, (field, answer)
    assert len(spawns) == 1, spawns
    command, timeout = spawns[0]
    assert command[-1] == "--version", command
    assert timeout <= VERSION_CHECK_CEILING_S, timeout


@pytest.mark.parametrize("backend", BACKENDS)
def test_info_names_a_backend_that_is_not_installed(tmp_path: Path, backend: str) -> None:
    answer = call(bench(tmp_path, backend, debugger_executable=tmp_path / "absent" / backend), INFO)

    assert answer["ok"] is False, answer
    assert answer["error_type"] == "debugger_not_found", answer
    assert "config_status" in answer, answer


@pytest.mark.parametrize("backend", BACKENDS)
def test_info_answers_timeout_when_the_version_command_does_not_return(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    hung = CompletedCommand(stdout="", stderr="", returncode=None, timed_out=True, not_found=False)
    monkeypatch.setattr(BACKEND_MODULES[backend], "spawn_command", lambda command, cwd, timeout_seconds: hung)
    answer = call(bench(tmp_path, backend), INFO)

    assert answer["ok"] is False, answer
    assert answer["error_type"] == "timeout", answer


# --- debugger_probes_list -------------------------------------------------------------


@pytest.mark.parametrize("backend", ["stlink", "pyocd"])
def test_probes_lists_probe_ids_from_the_cli_without_connecting_to_a_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    spawns = recorded_spawns(monkeypatch, backend)
    answer = call(bench(tmp_path, backend), PROBES)

    assert answer["ok"] is True, answer
    assert answer["probes"], answer
    assert all("probe_id" in probe for probe in answer["probes"]), answer
    assert answer.get("target_contacted") is not True, answer
    assert len(spawns) == 1, spawns
    command = spawns[0][0]
    if backend == "stlink":
        assert "-l" in command and not any(part.startswith("port=") for part in command), command
    else:
        assert "--probes" in command and "commander" not in command, command


def test_openocd_probes_reads_the_usb_inventory_and_never_reports_a_complete_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spawns = recorded_spawns(monkeypatch, "openocd")
    monkeypatch.setattr(openocd_backend, "list_available_com_ports", lambda tool: {"ok": True, "tool": tool, "ports": []})
    answer = call(bench(tmp_path, "openocd"), PROBES)

    assert answer["ok"] is True, answer
    assert answer["complete"] is False, answer
    assert answer["discovered_by"] == "usb_serial_inventory", answer
    assert answer["target_contacted"] is False, answer
    assert spawns == [], spawns


def test_openocd_probes_refuses_an_adapter_the_usb_inventory_cannot_read(tmp_path: Path) -> None:
    answer = call(bench(tmp_path, "openocd", interface_cfg="interface/cmsis-dap.cfg"), PROBES)

    assert answer["ok"] is False, answer
    assert answer["error_type"] == "not_supported", answer


@pytest.mark.parametrize("backend", ["stlink", "openocd"])
def test_probes_answers_probe_discovery_failed_when_the_listing_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    if backend == "openocd":
        monkeypatch.setattr(openocd_backend, "list_available_com_ports", lambda tool: {"ok": False, "tool": tool, "summary": "no serial backend"})
        service = bench(tmp_path, backend)
    else:
        service = bench(tmp_path, backend, debugger_executable=FAKE_STLINK_NO_PROBE)
    answer = call(service, PROBES)

    assert answer["ok"] is False, answer
    assert answer["error_type"] == "probe_discovery_failed", answer


# --- probe_target ---------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_probe_confirms_the_board_and_sends_no_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    spawns = recorded_spawns(monkeypatch, backend)
    answer = call(bench(tmp_path, backend), PROBE)

    assert answer["ok"] is True, answer
    assert answer["target_detected"] is True, answer
    assert spawns, spawns
    if backend == "openocd":
        commands = openocd_commands(spawns)
        assert re.search(r"\btargets\b", commands[-1]), commands
        assert not any(re.search(RESET_WORD, command) for command in commands), commands
    elif backend == "stlink":
        command = spawns[-1][0]
        assert "mode=HOTPLUG" in command, command
        assert not any(part in {"-rst", "-hardRst", "-halt"} or part.startswith("reset=") for spawned, _ in spawns for part in spawned), spawns
    else:
        commands = pyocd_commands(spawns)
        assert commands == ["status"], commands


@pytest.mark.parametrize(
    ("backend", "executable", "error_type"),
    [
        ("openocd", FAKE_OPENOCD_NO_PROBE, "adapter_not_found"),
        ("stlink", FAKE_STLINK_NO_PROBE, "adapter_not_found"),
        ("openocd", FAKE_OPENOCD_NO_TARGET, "target_not_detected"),
        ("stlink", FAKE_STLINK_NO_TARGET, "target_not_detected"),
    ],
    ids=["openocd-no-probe", "stlink-no-probe", "openocd-no-target", "stlink-no-target"],
)
def test_probe_names_a_missing_probe_and_a_silent_board_apart(tmp_path: Path, backend: str, executable: Path, error_type: str) -> None:
    answer = call(bench(tmp_path, backend, debugger_executable=executable), PROBE)

    assert answer["ok"] is False, answer
    assert answer["error_type"] == error_type, answer


def test_an_open_session_refuses_probe_and_reset_until_it_stops(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service)["ok"] is True
        probed = service.call(PROBE)
        reset = service.call(RESET, {"mode": "run"})
        assert service.call("debug_stop_session")["ok"] is True
        probed_after = service.call(PROBE)
        reset_after = service.call(RESET, {"mode": "run"})
    finally:
        service.close()

    for answer in (probed, reset):
        assert answer["ok"] is False, answer
        assert answer["error_type"] == "resource_busy", answer
    assert probed_after["ok"] is True, probed_after
    assert reset_after["ok"] is True, reset_after


# --- reset_target ---------------------------------------------------------------------


def test_reset_needs_allow_reset(tmp_path: Path) -> None:
    answer = call(bench(tmp_path, "openocd", permissions={**DEFAULT_TEST_PERMISSIONS, "allow_reset": False}), RESET)

    assert answer["ok"] is False, answer
    assert answer["error_type"] == "permission_denied", answer


FLASH_WORDS = r"\b(?:program|flash|erase|load|load_image|write_image)\b"


@pytest.mark.parametrize("backend", BACKENDS)
def test_reset_defaults_to_run_and_sends_nothing_that_writes_flash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    spawns = recorded_spawns(monkeypatch, backend)
    service = bench(tmp_path, backend)
    try:
        answer = service.call(RESET)
    finally:
        service.close()

    assert answer["ok"] is True, answer
    assert answer["mode"] == "run", answer
    if backend == "openocd":
        commands = openocd_commands(spawns)
        assert re.search(r"\breset run\b", commands[-1]), commands
        assert not any(re.search(FLASH_WORDS, command) for command in commands), commands
    elif backend == "stlink":
        command = spawns[-1][0]
        assert "-rst" in command, command
        assert not any(re.fullmatch(r"-(?:w\d*|e|d)|--(?:write|erase|download)", part) for part in command), command
    else:
        commands = pyocd_commands(spawns)
        assert commands == ["reset"], commands
        assert not any(part in {"flash", "erase", "load"} for command, _ in spawns for part in command), spawns


@pytest.mark.parametrize("backend", BACKENDS)
def test_reset_halt_stops_the_core_at_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    spawns = recorded_spawns(monkeypatch, backend)
    answer = call(bench(tmp_path, backend), RESET, {"mode": "halt"})

    assert answer["ok"] is True, answer
    assert answer["mode"] == "halt", answer
    if backend == "openocd":
        assert re.search(r"\breset halt\b", openocd_commands(spawns)[-1]), spawns
    elif backend == "stlink":
        assert "-halt" in spawns[-1][0], spawns
    else:
        assert pyocd_commands(spawns) == ["reset halt"], spawns


@pytest.mark.parametrize(("backend", "ok"), [("openocd", True), ("stlink", False), ("pyocd", False)])
def test_reset_init_runs_on_openocd_alone(tmp_path: Path, backend: str, ok: bool) -> None:
    answer = call(bench(tmp_path, backend), RESET, {"mode": "init"})

    assert answer["ok"] is ok, answer
    if not ok:
        assert answer["error_type"] == "not_supported", answer
        assert answer["target_contacted"] is False, answer


# --- the two symbol reads -----------------------------------------------------------------


def openocd_session(tmp_path: Path, **config_kwargs) -> AgenticHILToolService:
    service = debug_service(tmp_path, **config_kwargs)
    assert start_debug_session(service)["ok"] is True
    return service


def stlink_flashed(tmp_path: Path, **config_kwargs) -> AgenticHILToolService:
    service = stlink_dump_service(tmp_path, **config_kwargs)
    assert flash_symbol_source(service)["ok"] is True
    return service


def pyocd_flashed(tmp_path: Path, **config_kwargs) -> AgenticHILToolService:
    service = pyocd_read_service(tmp_path, **config_kwargs)
    assert flash_symbol_source(service)["ok"] is True
    return service


READY = [openocd_session, stlink_flashed, pyocd_flashed]
READY_IDS = ["openocd-session", "stlink-flashed-elf", "pyocd-flashed-elf"]


@pytest.mark.parametrize("prepare", READY, ids=READY_IDS)
def test_the_value_read_decodes_integers_only_at_the_widths_that_have_one(tmp_path: Path, prepare: Callable[..., AgenticHILToolService]) -> None:
    service = prepare(tmp_path)
    try:
        counter = service.call(VALUE, {"symbol": "boot_counter"})
        array = service.call(VALUE, {"symbol": "CTC_array"})
    finally:
        service.close()

    assert counter["ok"] is True, counter
    assert counter["size_bytes"] == BOOT_COUNTER_SIZE in INTEGER_VALUE_WIDTHS, counter
    assert len(counter["hex"]) == 2 * BOOT_COUNTER_SIZE, counter
    assert isinstance(counter["value_unsigned"], int) and isinstance(counter["value_signed"], int), counter
    assert array["ok"] is True, array
    assert array["size_bytes"] == CTC_ARRAY_SIZE not in INTEGER_VALUE_WIDTHS, array
    assert len(array["hex"]) == 2 * CTC_ARRAY_SIZE, array
    assert "value_unsigned" not in array and "value_signed" not in array and "value_not_decoded" in array, array


@pytest.mark.parametrize("tool", READS)
@pytest.mark.parametrize("prepare", READY, ids=READY_IDS)
def test_both_reads_refuse_a_symbol_over_the_size_cap(tmp_path: Path, prepare: Callable[..., AgenticHILToolService], tool: str) -> None:
    service = prepare(tmp_path, max_dump_size_bytes=BOOT_COUNTER_SIZE - 1)
    answer = call(service, tool, {"symbol": "boot_counter", "output_path": "build/capped.hex"} if tool == DUMP else {"symbol": "boot_counter"})

    assert answer["ok"] is False, answer
    assert answer["error_type"] == "permission_denied", answer
    assert "max_dump_size_bytes" in answer["summary"], answer
    assert not (tmp_path / "build" / "capped.hex").exists()


def openocd_without_a_session(tmp_path: Path) -> AgenticHILToolService:
    return debug_service(tmp_path)


@pytest.mark.parametrize("tool", READS)
@pytest.mark.parametrize(
    ("prepare", "error_type"),
    [(openocd_without_a_session, "session_not_active"), (stlink_dump_service, "symbol_source_not_available"), (pyocd_read_service, "symbol_source_not_available")],
    ids=["openocd-no-session", "stlink-nothing-flashed", "pyocd-nothing-flashed"],
)
def test_both_reads_name_the_missing_prerequisite_of_each_backend(tmp_path: Path, prepare: Callable[[Path], AgenticHILToolService], error_type: str, tool: str) -> None:
    answer = call(prepare(tmp_path), tool, {**ARGUMENTS[tool], "output_path": "build/early.hex"} if tool == DUMP else ARGUMENTS[tool])

    assert answer["ok"] is False, answer
    assert answer["error_type"] == error_type, answer


@pytest.mark.parametrize("tool", READS)
@pytest.mark.parametrize(
    ("symbol", "config_kwargs", "error_type"),
    [("boot_counter", {"allowed_symbols": ["CTC_array"]}, "permission_denied"), ("missing_symbol", {}, "symbol_not_found")],
    ids=["not-allowed", "not-in-the-elf"],
)
def test_both_reads_refuse_a_symbol_not_allowed_or_not_in_the_elf(tmp_path: Path, symbol: str, config_kwargs: dict, error_type: str, tool: str) -> None:
    arguments = {"symbol": symbol, "output_path": "build/refused.hex"} if tool == DUMP else {"symbol": symbol}
    answer = call(openocd_session(tmp_path, **config_kwargs), tool, arguments)

    assert answer["ok"] is False, answer
    assert answer["error_type"] == error_type, answer


def is_intel_hex(path: Path) -> bool:
    lines = path.read_text(encoding="ascii").split()
    return bool(lines) and all(re.fullmatch(r":[0-9A-Fa-f]{10,}", line) for line in lines) and lines[-1].upper() == ":00000001FF"


@pytest.mark.parametrize("prepare", READY, ids=READY_IDS)
def test_the_dump_writes_intel_hex_creating_folders_and_replacing_a_file(tmp_path: Path, prepare: Callable[..., AgenticHILToolService]) -> None:
    existing = tmp_path / "build" / "old.hex"
    service = prepare(tmp_path)
    existing.write_text("not a hex file\n", encoding="ascii")
    try:
        nested = service.call(DUMP, {"symbol": "CTC_array", "output_path": "build/new/nested/memory.hex"})
        replaced = service.call(DUMP, {"symbol": "boot_counter", "output_path": "build/old.hex"})
    finally:
        service.close()

    for answer, path in ((nested, tmp_path / "build" / "new" / "nested" / "memory.hex"), (replaced, existing)):
        assert answer["ok"] is True, answer
        for field in ("address", "size_bytes", "output"):
            assert field in answer, (field, answer)
        assert Path(answer["output"]["resolved_path"]) == path.resolve(), answer
        assert is_intel_hex(path), path.read_text(encoding="ascii")


@pytest.mark.parametrize(
    "output_path",
    ["build/../build/escape.hex", "OUTSIDE", "build/memory.bin", "elsewhere/memory.hex"],
    ids=["traversal", "outside-the-workspace", "not-hex", "outside-the-allowed-roots"],
)
def test_the_dump_refuses_an_output_path_the_rules_reject(tmp_path: Path, output_path: str) -> None:
    workspace = tmp_path / "bench"
    if output_path == "OUTSIDE":
        output_path = str(tmp_path / "outside.hex")
    answer = call(openocd_session(workspace), DUMP, {"symbol": "boot_counter", "output_path": output_path})

    assert answer["ok"] is False, answer
    assert answer["error_type"] == "output_validation_failed", answer
    assert not (tmp_path / "outside.hex").exists()
    assert not (workspace / "build" / "memory.bin").exists()
    assert not (workspace / "elsewhere").exists()


def test_every_output_path_example_the_definition_offers_is_accepted(tmp_path: Path, listed: dict[str, dict]) -> None:
    offered = examples(property_text(listed, DUMP, "output_path"))
    service = openocd_session(tmp_path)
    try:
        answers = {example: service.call(DUMP, {"symbol": "boot_counter", "output_path": example}) for example in offered}
    finally:
        service.close()

    for example, answer in answers.items():
        assert answer["ok"] is True, (example, answer)
