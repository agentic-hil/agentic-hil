"""What `tools/list` tells an agent about `debug_symbol_info` (#627).

The definition is read the way a host reads it: through a `tools/list` request
the server answers. Every claim it has to carry is paired with the behaviour
that makes it true, produced through `tools/call` against the fake debuggers,
so a definition that names an outcome the code no longer gives fails here as
surely as one that leaves an outcome out.

The checks are about meaning, never about the sentence that carries it. A
keyword on its own would accept its own inversion ("both required" for
"either", "needs no session" for "needs a session"), so each check is a
relation inside one clause or sentence, plus a negative check for the inverted
claim. Each check is itself tested against correct paraphrases it must accept
and misleading counterexamples it must reject.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from test_debug_backend_refusals import OK_HEX
from test_debug_sessions import (
    BOOT_COUNTER_SYMBOL_TABLE,
    debug_service,
    flash_symbol_source,
    pyocd_read_service,
    start_debug_session,
    stlink_dump_service,
)
from test_gdbserver_sessions import pyocd_session_service, st_link_session_service
from test_mcp_envelope import TOOLS_LIST, real_service, tools_call

from agentic_hil.backends.gdbdebug import DEBUG_SYMBOL_PATTERN
from agentic_hil.config import debug_interface_config
from agentic_hil.contracts import SYMBOL_NAME, validate_tool_arguments
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import AgenticHILToolService

TOOL = "debug_symbol_info"
# The fields of a resolution an agent acts on, returned on every backend.
RESULT_FIELDS = ("address", "size_bytes", "resolved_from")


# --- reading the definition ---------------------------------------------------


@pytest.fixture
def listed(tmp_path: Path) -> dict[str, dict]:
    """Every listed tool by name, as a `tools/list` response carries it."""
    service = real_service(tmp_path)
    try:
        response = handle_mcp_message(TOOLS_LIST, service)
    finally:
        service.close()
    assert isinstance(response, dict), response
    return {tool["name"]: tool for tool in response["result"]["tools"]}


def tool_text(listed: dict[str, dict]) -> str:
    return listed[TOOL]["description"]


def symbol_property(listed: dict[str, dict]) -> dict:
    return listed[TOOL]["inputSchema"]["properties"]["symbol"]


def symbol_text(listed: dict[str, dict]) -> str:
    return str(symbol_property(listed).get("description", ""))


def definition_text(listed: dict[str, dict]) -> str:
    """Everything a host shows an agent about this tool in words."""
    return tool_text(listed) + "\n" + symbol_text(listed)


# --- the meaning checks -------------------------------------------------------
#
# A sentence ends at a full stop followed by a space or the end; a clause also
# ends at a semicolon. "e.g." and "i.e." are not sentence ends.

NEGATED_NEED = r"\b(?:needs?|requires?)\s+no\b|\b(?:does\s+not|doesn't|do\s+not|never)\s+(?:need|require)|\boptional\b|\bnot\s+(?:needed|required)\b"


def _unabbreviated(text: str) -> str:
    return re.sub(r"\b(e)\.g\.|\b(i)\.e\.", lambda match: (match.group(1) or match.group(2)) + "g", text)


def sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\.(?=\s|$)|\n", _unabbreviated(text)) if part.strip()]


def clauses(text: str) -> list[str]:
    return [part.strip() for sentence in sentences(text) for part in sentence.split(";") if part.strip()]


def clauses_with(text: str, *patterns: str) -> list[str]:
    return [clause for clause in clauses(text) if all(re.search(pattern, clause, re.IGNORECASE) for pattern in patterns)]


def names(text: str, term: str) -> bool:
    return re.search(rf"(?<![\w.]){re.escape(term)}(?![\w])", text) is not None


def names_symbol_syntax(text: str) -> bool:
    """A C identifier, `::` qualification optional, and no expressions."""
    if not re.search(r"\bidentifier", text, re.IGNORECASE):
        return False
    qualified = clauses_with(text, r"::")
    optional = any(re.search(r"\boptional(?:ly)?\b|\bmay\b|\bcan\b|\bor\b", clause, re.IGNORECASE) for clause in qualified)
    forced = any(
        re.search(r"\b(?:must|always)\b[^,;]{0,25}::|::[^,;]{0,25}\b(?:required|mandatory|always)\b|\brequires?\b[^,;]{0,25}::", clause, re.IGNORECASE)
        for clause in qualified
    )
    expressions_offered = any(not re.search(r"\b(?:no|not|never|without)\b", clause, re.IGNORECASE) for clause in clauses_with(text, r"\bexpressions?\b"))
    return optional and not forced and not expressions_offered


EXAMPLE_MARKER = re.compile(r"(?:\be\.g\.|\bfor example|\bsuch as|\bas in|\bexample)[:,]?\s+", re.IGNORECASE)


def examples(text: str) -> list[str]:
    """Each complete example the text offers, up to its delimiter, never a prefix of it."""
    found = []
    for marker in EXAMPLE_MARKER.finditer(text):
        offered = re.split(r"[;)]|\.(?=\s|$)", text[marker.end() :], maxsplit=1)[0]
        for item in re.split(r",|\s+or\s+|\s+and\s+", offered):
            item = item.strip().strip("`'\"").strip()
            if item:
                found.append(item)
    return found


def schema_accepts(symbol: str) -> bool:
    return validate_tool_arguments(TOOL, {"symbol": symbol}) is None and DEBUG_SYMBOL_PATTERN.match(symbol) is not None


def gives_valid_examples(text: str) -> bool:
    offered = examples(text)
    return bool(offered) and all(schema_accepts(example) for example in offered)


def names_lookup_context(text: str) -> bool:
    """The purpose sentence says where the symbol is looked up."""
    first = sentences(text)[0] if sentences(text) else ""
    return re.search(r"\bELF\b|\bsymbol\s+table\b|\bdebug\s+info(?:rmation)?\b|\bfirmware\s+image\b", first, re.IGNORECASE) is not None


def allows_either_policy_key(text: str) -> bool:
    """Listed in `debug.allowed_symbols` OR `debug.allow_all_symbols` true, never both required."""
    both = clauses_with(text, r"debug\.allowed_symbols", r"debug\.allow_all_symbols")
    joint = r"\bboth\b|\ball\s+of\b|allowed_symbols\W+and\W+(?:debug\.)?allow_all_symbols|allow_all_symbols\W+(?:is\s+true\s+)?and\W+(?:debug\.)?allowed_symbols"
    either = r"allowed_symbols.*\bor\b.*allow_all_symbols|allow_all_symbols.*\bor\b.*allowed_symbols"
    return any(re.search(either, clause) for clause in both) and not any(re.search(joint, clause, re.IGNORECASE) for clause in both)


def allow_all_grants_when_true(text: str) -> bool:
    keyed = clauses_with(text, r"allow_all_symbols")
    granted = any(re.search(r"allow_all_symbols\W+(?:is\s+|=\s*|set\s+to\s+|:\s*)?true\b", clause, re.IGNORECASE) for clause in keyed)
    inverted = any(re.search(r"allow_all_symbols\W+(?:is\s+|=\s*|set\s+to\s+|:\s*)?false\b", clause, re.IGNORECASE) for clause in keyed)
    return granted and not inverted


def states_allow_all_default_false(text: str) -> bool:
    """The loader's default, which is what a config that leaves the key out gets."""
    keyed = clauses_with(text, r"allow_all_symbols")
    false_default = any(re.search(r"\bdefaults?\b\W*(?:is\s+|to\s+)?false\b|\bfalse\s+by\s+default\b", clause, re.IGNORECASE) for clause in keyed)
    true_default = any(re.search(r"\bdefaults?\b\W*(?:is\s+|to\s+)?true\b|\btrue\s+by\s+default\b", clause, re.IGNORECASE) for clause in keyed)
    return false_default and not true_default


# A session said to be missing ("without one", "with no session"), and one said
# to be there ("while a session is open"), as the condition of a claim (#698).
NO_SESSION = r"\b(?:without|with\s+no|when\s+no|if\s+no|no)\s+(?:(?:an?|open|active|debug)\s+)*(?:session|debug_start_session|one)\b"
SESSION_PRESENT = r"\b(?:with|while|when|if|in|inside|during)\s+(?:an?\s+|the\s+)?(?:open\s+|active\s+)?(?:debug\s+)?session\b(?!_)|\bsession\s+(?:is\s+)?(?:open|active)\b|\beven\b"


def _comma_parts(clause: str) -> list[str]:
    return [part.strip() for part in clause.split(",") if part.strip()]


def openocd_needs_debug_start_session(text: str) -> bool:
    candidates = clauses_with(text, r"\bOpenOCD\b", r"\bdebug_start_session\b")
    return any(
        re.search(r"\b(?:needs?|requires?|must|first|active|only)\b", clause, re.IGNORECASE) and not re.search(NEGATED_NEED, clause, re.IGNORECASE)
        for clause in candidates
    )


def openocd_needs_a_session(text: str) -> bool:
    """OpenOCD needs debug_start_session, or, in the words of #698, refuses with
    session_not_active when no session is open.

    The second form binds the refusal to OpenOCD in one comma-separated part and
    takes its condition from that part or one before it in the same clause, so
    "without one, OpenOCD refuses (session_not_active)" is the claim and "with
    an open session OpenOCD refuses" is its inversion.
    """
    needed = openocd_needs_debug_start_session(text)
    for clause in clauses_with(text, r"\bOpenOCD\b", r"\bsession_not_active\b"):
        found = _comma_parts(clause)
        for index, part in enumerate(found):
            if not (re.search(r"\bOpenOCD\b", part) and names(part, "session_not_active")):
                continue
            condition = any(re.search(NO_SESSION, earlier, re.IGNORECASE) for earlier in found[: index + 1])
            inverted = re.search(SESSION_PRESENT, part, re.IGNORECASE) or re.search(r"\b(?:never|not|doesn't|does\s+not|no\s+longer)\b", part, re.IGNORECASE)
            if condition and not inverted:
                needed = True
    return needed


def sessionless_backends_need_this_servers_flashed_elf(text: str) -> bool:
    candidates = clauses_with(text, r"\bpyOCD\b", r"\bSTM32CubeProgrammer\b", r"\bELF\b", r"\bflash_firmware\b")
    return any(
        re.search(r"\blast\b|\bmost\s+recent(?:ly)?\b|\bthis\s+(?:server|service|session|process)\b", clause, re.IGNORECASE)
        and not re.search(NEGATED_NEED, clause, re.IGNORECASE)
        for clause in candidates
    )


EVERY_BACKEND = r"\b(?:every|each|any|all)\s+(?:three\s+)?backends?\b|\ball\s+three\b"
SESSION_OPEN = r"\b(?:open|active)\s+(?:debug\s+)?(?:debug_start_session|session)\b|\b(?:debug_start_session|session)\b(?!_)[^,;]{0,20}\b(?:is\s+)?(?:open|active)\b"
SESSION_ANSWERS = r"\b(?:answers?|serves?|reads?|resolves?|is\s+used|goes\s+through|runs?)\b"
LIMITED = r"\bonly\b|\bno\s+backend\b|\bnot\s+(?:on\s+)?(?:every|each|all|any)\b|\bnever\b|\bexcept\b|\bignor\w*"


# The same claim backend by backend: OpenOCD needs the session and the other two
# use it while it is open ("pyOCD and STM32CubeProgrammer use the session if
# open, else the ELF flash_firmware last wrote", or "use an open session, else").
IF_OPEN = r"\b(?:if|when|while)\s+(?:it\s+is\s+|one\s+is\s+|the\s+session\s+is\s+)?(?:open|active)\b"
USES_THE_SESSION = r"\b(?:use|uses|read|reads|answer|answers|resolve|resolves)\s+(?:through\s+|from\s+|via\s+)?(?:it|one|the\s+session|a\s+session|debug_start_session)\b"
USES_AN_OPEN_SESSION = r"\b(?:use|uses|read|reads|answer|answers|resolve|resolves)\s+(?:through\s+|from\s+|via\s+)?(?:an|the)\s+(?:open|active)\s+(?:debug\s+)?(?:session|debug_start_session)\b"
SESSION_UNUSED = r"\b(?:never|not|don't|doesn't|ignores?|ignored)\b"


def sessionless_backends_use_an_open_session(part: str) -> bool:
    """One comma-separated part: pyOCD and STM32CubeProgrammer read through the session while it is open."""
    uses_it_while_open = (
        re.search(USES_THE_SESSION, part, re.IGNORECASE) is not None and re.search(IF_OPEN, part, re.IGNORECASE) is not None
    ) or re.search(USES_AN_OPEN_SESSION, part, re.IGNORECASE) is not None
    return (
        all(re.search(name, part) for name in (r"\bpyOCD\b", r"\bSTM32CubeProgrammer\b"))
        and uses_it_while_open
        and re.search(SESSION_UNUSED, part, re.IGNORECASE) is None
    )


def an_open_session_answers_on_every_backend(text: str) -> bool:
    """While a session is open it answers, on every backend (#698), never on some only.

    Said at once ("an open debug_start_session answers on every backend") or
    backend by backend: OpenOCD needs debug_start_session and pyOCD and
    STM32CubeProgrammer use the session while it is open.
    """
    at_once = any(
        re.search(SESSION_OPEN, clause, re.IGNORECASE)
        and re.search(SESSION_ANSWERS, clause, re.IGNORECASE)
        and re.search(EVERY_BACKEND, clause, re.IGNORECASE)
        and not re.search(LIMITED, clause, re.IGNORECASE)
        for clause in clauses(text)
    )
    by_backend = openocd_needs_debug_start_session(text) and any(
        sessionless_backends_use_an_open_session(part) for clause in clauses(text) for part in _comma_parts(clause)
    )
    return at_once or by_backend


def flashed_elf_only_without_a_session(text: str) -> bool:
    """pyOCD and STM32CubeProgrammer take the ELF flash_firmware last wrote when no
    session is open (#698), never in spite of one.

    The condition belongs to the part that names the two backends, or to a part
    before it in the same clause ("without one, OpenOCD refuses, pyOCD and
    STM32CubeProgrammer use the ELF"), so the unconditional claim fails. The
    ELF may also be the "else" of the two backends using an open session
    ("use the session if open, else the ELF flash_firmware last wrote").
    """
    for clause in clauses(text):
        found = _comma_parts(clause)
        for index, part in enumerate(found):
            if (
                index > 0
                and re.match(r"(?:else|otherwise)\b", part, re.IGNORECASE)
                and re.search(r"\bflash_firmware\b", part)
                and not re.search(r"\balways\b", part, re.IGNORECASE)
                and sessionless_backends_use_an_open_session(found[index - 1])
            ):
                return True
            if not all(re.search(term, part) for term in (r"\bpyOCD\b", r"\bSTM32CubeProgrammer\b", r"\bflash_firmware\b")):
                continue
            condition = any(re.search(NO_SESSION, earlier, re.IGNORECASE) for earlier in found[: index + 1])
            inverted = any(re.search(SESSION_PRESENT, earlier, re.IGNORECASE) or re.search(r"\balways\b", earlier, re.IGNORECASE) for earlier in found[: index + 1])
            if condition and not inverted:
                return True
    return False


def names_result_fields_and_units(text: str) -> bool:
    if not all(names(text, field) for field in RESULT_FIELDS):
        return False
    hex_address = any(re.search(r"\bhex(?:adecimal)?\b|\b0x", clause, re.IGNORECASE) for clause in clauses_with(text, r"\baddress\b"))
    size_not_bytes = any(re.search(r"\b(?:bits?|words?|kilobytes?|KiB|KB)\b", clause) for clause in clauses_with(text, r"\bsize_bytes\b"))
    source_given_a_unit = re.search(r"\bresolved_from\s*(?:\(\s*|\b(?:as|in|is)\s+)(?:an?\s+)?(?:bytes?|bits?|hex|address|integer)\b", text, re.IGNORECASE)
    return hex_address and not size_not_bytes and source_given_a_unit is None


SIBLING_MEANINGS = {
    "debug_symbol_value": r"\bbytes?\b|\bvalues?\b|\bcontents?\b",
    "debug_dump_symbol_ihex": r"\bIntel\s+HEX\b|\bHEX\s+file\b|\.hex\b",
}
NEGATION = r"\b(?:no|not|never|without|nothing)\b"


def _segments(text: str) -> list[str]:
    return [part.strip() for clause in clauses(text) for part in re.split(r",|\s+and\s+|\s+while\s+", clause) if part.strip()]


def _says_what_the_sibling_does(segment: str, sibling: str, meaning: str) -> bool:
    """The sibling's segment names what it does, and does not deny it.

    The denial is looked for after the name ("debug_dump_symbol_ihex never
    writes") and right before it ("do not use debug_symbol_value"), so a
    negation that belongs to this tool's own claim earlier in the segment
    ("reads no memory: debug_symbol_value reads its bytes") is not taken for it.
    """
    found = re.search(rf"(?<![\w.]){re.escape(sibling)}(?![\w])", segment)
    if found is None or not re.search(meaning, segment, re.IGNORECASE):
        return False
    head, tail = segment[: found.start()], segment[found.end() :]
    denied_after = re.search(NEGATION, tail, re.IGNORECASE)
    denied_before = re.search(r"\b(?:not|never|no|don't)\s+(?:use\s+|call\s+)?$", head, re.IGNORECASE)
    return not denied_after and not denied_before


def tells_it_apart_from_the_memory_reads(text: str) -> bool:
    """This tool reads no target memory; the two siblings read it, for bytes and for Intel HEX."""
    segments = _segments(text)
    for sibling, meaning in SIBLING_MEANINGS.items():
        if not any(_says_what_the_sibling_does(segment, sibling, meaning) for segment in segments):
            return False
    for segment in segments:
        head = re.split("|".join(SIBLING_MEANINGS), segment, maxsplit=1)[0]
        if re.search(NEGATION, head, re.IGNORECASE) and re.search(r"\bmemory\b", head, re.IGNORECASE) and re.search(r"\b(?:reads?|reading|read|access(?:es)?|touch(?:es)?)\b", head, re.IGNORECASE):
            return True
    return False


# What makes each refusal happen, as the code decides it. A refusal named with
# no circumstance in its sentence tells an agent nothing it can act on.
CIRCUMSTANCES = {
    "permission_denied": r"allowed_symbols|allow_all_symbols|\bnot\s+(?:allowed|listed)\b",
    "session_not_active": r"\bdebug_start_session\b|\bsession\b",
    "symbol_source_not_available": r"\bflash_firmware\b|\bflashed\b",
    "symbol_source_changed": r"\b(?:changed?|changes|rebuilt|replaced|removed|deleted|modified|differs?)\b|\bno\s+longer\b",
    "symbol_not_found": r"\b(?:unknown|missing|absent)\b|\bnot\s+(?:found|in|present)\b|\bno\s+such\b",
}


def explains(text: str, error_type: str) -> bool:
    return any(
        names(sentence, error_type)
        and re.search(CIRCUMSTANCES[error_type], sentence, re.IGNORECASE)
        and not re.search(r"\bsuccess(?:ful)?\b|\bsucceeds?\b", sentence, re.IGNORECASE)
        for sentence in sentences(text)
    )


# --- the checks accept paraphrases and reject inversions ----------------------


@pytest.mark.parametrize(
    ("check", "text", "accepted"),
    [
        (names_symbol_syntax, "C identifier, optionally ::-qualified, e.g. boot_counter.", True),
        (names_symbol_syntax, "A C identifier or a C++ name qualified with ::, for example motor::speed.", True),
        (names_symbol_syntax, "C identifier that must be ::-qualified, e.g. app::boot_counter.", False),
        (names_symbol_syntax, "Any C expression or identifier, optionally ::-qualified.", False),
        (names_symbol_syntax, "Symbol name, e.g. boot_counter.", False),
        (gives_valid_examples, "C identifier, e.g. boot_counter.", True),
        (gives_valid_examples, "C identifier, for example `app::state`.", True),
        (gives_valid_examples, "(such as boot_counter or app::state)", True),
        (gives_valid_examples, "C identifier, e.g. boot_counter[0].", False),
        (gives_valid_examples, "C identifier, e.g. boot_counter().", False),
        (gives_valid_examples, "C identifier, e.g. app.state.", False),
        (gives_valid_examples, "C identifier, e.g. *ptr.", False),
        (gives_valid_examples, "C identifier.", False),
        (names_lookup_context, "Find an allowed symbol's address (hex) in the ELF. Reads no memory.", True),
        (names_lookup_context, "Looks up a symbol in the firmware's symbol table.", True),
        (names_lookup_context, "Find an allowed symbol's address. The ELF is used.", False),
        (allows_either_policy_key, "Allowed if in debug.allowed_symbols or debug.allow_all_symbols is true.", True),
        (allows_either_policy_key, "Allowed when debug.allow_all_symbols is true or the name is listed in debug.allowed_symbols.", True),
        (allows_either_policy_key, "debug.allowed_symbols and debug.allow_all_symbols are both required.", False),
        (allows_either_policy_key, "Needs debug.allow_all_symbols is true and debug.allowed_symbols listing it.", False),
        (allow_all_grants_when_true, "Allowed if in debug.allowed_symbols or debug.allow_all_symbols is true (default false).", True),
        (allow_all_grants_when_true, "Allowed if debug.allow_all_symbols = true.", True),
        (allow_all_grants_when_true, "Allowed if in debug.allowed_symbols or debug.allow_all_symbols is false.", False),
        (states_allow_all_default_false, "Allowed if in debug.allowed_symbols or debug.allow_all_symbols is true (default false).", True),
        (states_allow_all_default_false, "debug.allow_all_symbols is false by default.", True),
        (states_allow_all_default_false, "Allowed if in debug.allowed_symbols or debug.allow_all_symbols is true (default true).", False),
        (states_allow_all_default_false, "Allowed if debug.allow_all_symbols is true.", False),
        (openocd_needs_a_session, "OpenOCD needs an active debug_start_session (else session_not_active).", True),
        (openocd_needs_a_session, "Call debug_start_session first on OpenOCD.", True),
        (openocd_needs_a_session, "With OpenOCD it requires a session from debug_start_session.", True),
        (openocd_needs_a_session, "OpenOCD needs no debug_start_session.", False),
        (openocd_needs_a_session, "OpenOCD does not need debug_start_session first.", False),
        (openocd_needs_a_session, "debug_start_session is optional on OpenOCD, which is fine first.", False),
        (openocd_needs_a_session, "pyOCD needs debug_start_session first.", False),
        (openocd_needs_a_session, "An open session answers on every backend; without one, OpenOCD refuses with session_not_active.", True),
        (openocd_needs_a_session, "With no session OpenOCD refuses (session_not_active).", True),
        (openocd_needs_a_session, "With an open session OpenOCD refuses (session_not_active).", False),
        (openocd_needs_a_session, "Without one, OpenOCD never refuses with session_not_active.", False),
        (openocd_needs_a_session, "Without one, pyOCD refuses (session_not_active).", False),
        (openocd_needs_a_session, "OpenOCD refuses with session_not_active.", False),
        (an_open_session_answers_on_every_backend, "An open debug_start_session answers on every backend.", True),
        (an_open_session_answers_on_every_backend, "While a session is open, all three backends read through it.", True),
        (an_open_session_answers_on_every_backend, "On any backend an active session answers.", True),
        (an_open_session_answers_on_every_backend, "An open debug_start_session answers on OpenOCD only.", False),
        (an_open_session_answers_on_every_backend, "An open session answers on no backend.", False),
        (an_open_session_answers_on_every_backend, "Not every backend answers from an open session.", False),
        (an_open_session_answers_on_every_backend, "Every backend answers without a session.", False),
        (an_open_session_answers_on_every_backend, "An open session is ignored on every backend.", False),
        (
            an_open_session_answers_on_every_backend,
            "OpenOCD needs debug_start_session (else session_not_active); pyOCD and STM32CubeProgrammer use it if open, else the ELF flash_firmware last wrote.",
            True,
        ),
        (an_open_session_answers_on_every_backend, "OpenOCD needs debug_start_session; pyOCD and STM32CubeProgrammer read through the session while it is open.", True),
        (an_open_session_answers_on_every_backend, "OpenOCD needs debug_start_session; pyOCD and STM32CubeProgrammer ignore it if open.", False),
        (an_open_session_answers_on_every_backend, "OpenOCD needs debug_start_session; pyOCD and STM32CubeProgrammer never use it, even if open.", False),
        (an_open_session_answers_on_every_backend, "OpenOCD needs debug_start_session; pyOCD uses it if open.", False),
        (an_open_session_answers_on_every_backend, "OpenOCD needs debug_start_session; pyOCD and STM32CubeProgrammer use the session if closed.", False),
        (an_open_session_answers_on_every_backend, "OpenOCD does not need debug_start_session; pyOCD and STM32CubeProgrammer use it if open.", False),
        (an_open_session_answers_on_every_backend, "pyOCD and STM32CubeProgrammer use the session if open.", False),
        (
            flashed_elf_only_without_a_session,
            "An open session answers on every backend; without one, OpenOCD refuses (session_not_active), pyOCD and STM32CubeProgrammer use the ELF flash_firmware last wrote.",
            True,
        ),
        (flashed_elf_only_without_a_session, "With no session pyOCD and STM32CubeProgrammer use the ELF flash_firmware last wrote.", True),
        (flashed_elf_only_without_a_session, "pyOCD and STM32CubeProgrammer use the ELF flash_firmware last wrote.", False),
        (flashed_elf_only_without_a_session, "pyOCD and STM32CubeProgrammer use the ELF this server last flashed via flash_firmware (else symbol_source_not_available).", False),
        (flashed_elf_only_without_a_session, "Without a session or with one, pyOCD and STM32CubeProgrammer always use the ELF flash_firmware last wrote.", False),
        (flashed_elf_only_without_a_session, "Without one, OpenOCD refuses; with an open session, pyOCD and STM32CubeProgrammer use the ELF flash_firmware last wrote.", False),
        (flashed_elf_only_without_a_session, "pyOCD and STM32CubeProgrammer use the session if open, else the ELF flash_firmware last wrote.", True),
        (flashed_elf_only_without_a_session, "pyOCD and STM32CubeProgrammer use it while it is open, otherwise the ELF flash_firmware last wrote.", True),
        (flashed_elf_only_without_a_session, "pyOCD and STM32CubeProgrammer use the session if closed, else the ELF flash_firmware last wrote.", False),
        (flashed_elf_only_without_a_session, "pyOCD and STM32CubeProgrammer use the ELF flash_firmware last wrote, else the session if open.", False),
        (flashed_elf_only_without_a_session, "pyOCD and STM32CubeProgrammer use the session if open, and the ELF flash_firmware last wrote.", False),
        (flashed_elf_only_without_a_session, "pyOCD and STM32CubeProgrammer never use the session if open, else the ELF flash_firmware last wrote.", False),
        (flashed_elf_only_without_a_session, "pyOCD uses the session if open, else the ELF flash_firmware last wrote.", False),
        (sessionless_backends_need_this_servers_flashed_elf, "pyOCD and STM32CubeProgrammer use the ELF this server last flashed via flash_firmware.", True),
        (sessionless_backends_need_this_servers_flashed_elf, "On STM32CubeProgrammer or pyOCD it reads the ELF most recently flashed with flash_firmware.", True),
        (sessionless_backends_need_this_servers_flashed_elf, "pyOCD and STM32CubeProgrammer use any ELF; flash_firmware is optional.", False),
        (sessionless_backends_need_this_servers_flashed_elf, "pyOCD and STM32CubeProgrammer need no ELF from flash_firmware, not even the last one.", False),
        (sessionless_backends_need_this_servers_flashed_elf, "OpenOCD uses the ELF last flashed via flash_firmware.", False),
        (names_result_fields_and_units, "Find an allowed symbol's address (hex), size_bytes and resolved_from in the ELF.", True),
        (names_result_fields_and_units, "Returns the hexadecimal address, size_bytes and resolved_from.", True),
        (names_result_fields_and_units, "Returns the address as 0x text, size_bytes and resolved_from.", True),
        (names_result_fields_and_units, "Returns address (decimal), size_bytes and resolved_from.", False),
        (names_result_fields_and_units, "Returns the hex address, size_bytes in bits and resolved_from.", False),
        (names_result_fields_and_units, "Returns the hex address, size_bytes and resolved_from as bytes.", False),
        (tells_it_apart_from_the_memory_reads, "No memory read; for bytes use debug_symbol_value, for Intel HEX debug_dump_symbol_ihex.", True),
        (tells_it_apart_from_the_memory_reads, "Reads no target memory: debug_symbol_value reads its bytes and debug_dump_symbol_ihex writes them to an Intel HEX file.", True),
        (tells_it_apart_from_the_memory_reads, "No memory safety checks; for bytes use debug_symbol_value, for Intel HEX debug_dump_symbol_ihex.", False),
        (tells_it_apart_from_the_memory_reads, "Reads target memory; for bytes use debug_symbol_value, for Intel HEX debug_dump_symbol_ihex.", False),
        (tells_it_apart_from_the_memory_reads, "No memory read; for bytes use debug_symbol_value, debug_dump_symbol_ihex never writes Intel HEX.", False),
        (tells_it_apart_from_the_memory_reads, "debug_symbol_value reads no memory, for Intel HEX debug_dump_symbol_ihex.", False),
    ],
)
def test_each_check_accepts_a_paraphrase_and_rejects_an_inversion(check: Callable[[str], bool], text: str, accepted: bool) -> None:
    assert bool(check(text)) is accepted, (check.__name__, text)


@pytest.mark.parametrize(
    ("text", "error_type", "accepted"),
    [
        ("Allowed if in debug.allowed_symbols or debug.allow_all_symbols is true, else permission_denied.", "permission_denied", True),
        ("OpenOCD needs an active debug_start_session (else session_not_active).", "session_not_active", True),
        ("Else symbol_source_not_available: nothing was flashed with flash_firmware.", "symbol_source_not_available", True),
        ("symbol_source_changed if that ELF changed or was removed since.", "symbol_source_changed", True),
        ("Not in the ELF: symbol_not_found.", "symbol_not_found", True),
        ("Refusals are success: permission_denied, session_not_active, symbol_not_found.", "permission_denied", False),
        ("Refusals: permission_denied, session_not_active, symbol_not_found.", "symbol_not_found", False),
        ("symbol_source_changed is returned.", "symbol_source_changed", False),
    ],
)
def test_the_refusal_check_wants_the_circumstance_beside_the_code(text: str, error_type: str, accepted: bool) -> None:
    assert explains(text, error_type) is accepted, text


# --- the symbol parameter -----------------------------------------------------


def test_the_symbol_parameter_carries_its_own_description(listed: dict[str, dict]) -> None:
    assert symbol_text(listed).strip(), symbol_property(listed)


def test_the_symbol_description_names_the_syntax_the_schema_enforces(listed: dict[str, dict]) -> None:
    """An identifier, `::` qualification optional, no expressions: what the listed pattern accepts."""
    assert names_symbol_syntax(symbol_text(listed)), symbol_text(listed)


def test_the_symbol_description_gives_only_examples_the_schema_accepts(listed: dict[str, dict]) -> None:
    text = symbol_text(listed)
    assert examples(text), text
    for example in examples(text):
        assert schema_accepts(example), (example, text)


def test_the_definition_says_where_the_symbol_is_looked_up(listed: dict[str, dict]) -> None:
    assert names_lookup_context(tool_text(listed)), tool_text(listed)


def test_the_listed_schema_keeps_the_syntax_the_backends_enforce(listed: dict[str, dict]) -> None:
    """The description is added beside the constraint, never in place of it."""
    schema = listed[TOOL]["inputSchema"]
    prop = symbol_property(listed)
    assert prop["type"] == "string"
    assert prop["pattern"] == SYMBOL_NAME["pattern"] == DEBUG_SYMBOL_PATTERN.pattern
    assert schema["required"] == ["symbol"]
    assert schema["additionalProperties"] is False


# --- "allowed": the project's symbol policy ----------------------------------


def test_the_definition_says_a_symbol_is_allowed_by_either_policy_key(listed: dict[str, dict]) -> None:
    """Exact membership in the allowlist, or the grant set to true: either is enough."""
    text = definition_text(listed)
    assert allows_either_policy_key(text), text
    assert allow_all_grants_when_true(text), text


def test_the_definition_states_the_loader_default_of_the_grant(listed: dict[str, dict]) -> None:
    assert states_allow_all_default_false(definition_text(listed)), definition_text(listed)


@pytest.mark.parametrize(
    ("policy", "allowed"),
    [
        ({"allowed_symbols": ["boot_counter"], "allow_all_symbols": False}, True),
        ({"allowed_symbols": [], "allow_all_symbols": True}, True),
        ({"allowed_symbols": ["CTC_array"], "allow_all_symbols": False}, False),
        ({"allowed_symbols": [], "allow_all_symbols": False}, False),
    ],
    ids=["listed", "allow-all", "not-listed", "nothing-allowed"],
)
def test_either_key_alone_allows_a_symbol(tmp_path: Path, policy: dict, allowed: bool) -> None:
    service = stlink_dump_service(tmp_path, **policy)
    try:
        assert flash_symbol_source(service)["ok"] is True
        answer = call(service, "boot_counter")["structuredContent"]
    finally:
        service.close()

    if allowed:
        assert answer["ok"] is True, answer
    else:
        assert answer["ok"] is False, answer
        assert answer["error_type"] == "permission_denied", answer
        assert "address" not in answer, answer


@pytest.mark.parametrize("debug_section", [{}, {"allowed_symbols": ["CTC_array"]}], ids=["no-debug-keys", "allowlist-only"])
def test_a_config_that_leaves_the_grant_out_gets_false(debug_section: dict) -> None:
    """The loader default the definition states, for a debug section without the key."""
    assert debug_interface_config(debug_section).allow_all_symbols is False


# --- what must come first -----------------------------------------------------


def test_the_definition_ties_each_prerequisite_to_its_backend(listed: dict[str, dict]) -> None:
    """OpenOCD answers through the session; pyOCD and STM32CubeProgrammer from the flashed ELF."""
    text = tool_text(listed)
    assert openocd_needs_a_session(text), text
    assert sessionless_backends_need_this_servers_flashed_elf(text), text
    for tool in ("debug_start_session", "flash_firmware"):
        assert tool in listed, tool


def test_the_definition_says_an_open_session_answers_on_every_backend(listed: dict[str, dict]) -> None:
    """#698: an open session answers on every backend; without one, OpenOCD
    refuses with session_not_active while pyOCD and STM32CubeProgrammer resolve
    against the ELF flash_firmware last wrote. The session is still named by
    the tool that opens it."""
    text = tool_text(listed)
    assert an_open_session_answers_on_every_backend(text), text
    assert names(text, "debug_start_session"), text
    assert flashed_elf_only_without_a_session(text), text


# --- what it answers ----------------------------------------------------------


def test_the_definition_names_the_result_fields_and_their_units(listed: dict[str, dict]) -> None:
    assert names_result_fields_and_units(tool_text(listed)), tool_text(listed)


def call(service: AgenticHILToolService, symbol: str) -> dict:
    """A `debug_symbol_info` call the way a host makes it, answered as it sees it."""
    response = handle_mcp_message(tools_call(7, TOOL, {"symbol": symbol}), service)
    assert isinstance(response, dict), response
    return response["result"]


def openocd_session(tmp_path: Path) -> AgenticHILToolService:
    service = debug_service(tmp_path)
    assert start_debug_session(service)["ok"] is True
    return service


def stlink_flashed(tmp_path: Path) -> AgenticHILToolService:
    service = stlink_dump_service(tmp_path)
    assert flash_symbol_source(service)["ok"] is True
    return service


def pyocd_flashed(tmp_path: Path) -> AgenticHILToolService:
    service = pyocd_read_service(tmp_path)
    assert flash_symbol_source(service)["ok"] is True
    return service


@pytest.mark.parametrize(
    "prepare",
    [openocd_session, stlink_flashed, pyocd_flashed],
    ids=["openocd-session", "stlink-flashed-elf", "pyocd-flashed-elf"],
)
def test_every_backend_answers_with_the_named_fields(tmp_path: Path, prepare: Callable[[Path], AgenticHILToolService]) -> None:
    service = prepare(tmp_path)
    try:
        result = call(service, "boot_counter")
    finally:
        service.close()

    answer = result["structuredContent"]
    assert result["isError"] is False, answer
    for field in RESULT_FIELDS:
        assert field in answer, (field, answer)
    assert answer["address"].startswith("0x"), answer
    int(answer["address"], 16)
    assert isinstance(answer["size_bytes"], int), answer
    assert answer["resolved_from"] in {"debug_info", "elf_symbol_table"}, answer


# --- how it differs from the two reads ----------------------------------------


def test_the_definition_tells_it_apart_from_the_tools_that_read_memory(listed: dict[str, dict]) -> None:
    text = tool_text(listed)
    assert tells_it_apart_from_the_memory_reads(text), text
    for sibling in SIBLING_MEANINGS:
        assert sibling in listed, sibling


SESSION_BACKENDS = ("openocd", "pyocd", "stlink")
# What each backend answers once the session is gone and this server flashed nothing.
WITHOUT_A_SESSION = {"openocd": "session_not_active", "pyocd": "symbol_source_not_available", "stlink": "symbol_source_not_available"}


def session_on(backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AgenticHILToolService, dict]:
    """A session opened with `attach` on `backend`, with nothing flashed by this server."""
    if backend == "openocd":
        service = debug_service(tmp_path)
    elif backend == "pyocd":
        service, _ = pyocd_session_service(tmp_path, monkeypatch, elf_symbols=BOOT_COUNTER_SYMBOL_TABLE)
    else:
        service, _ = st_link_session_service(tmp_path, monkeypatch, elf_symbols=BOOT_COUNTER_SYMBOL_TABLE)
    started = start_debug_session(service, mode="attach")
    assert started["ok"] is True, started
    return service, started


@pytest.mark.parametrize("backend", SESSION_BACKENDS)
def test_an_open_session_answers_on_every_backend_and_without_one_each_backend_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    """The behaviour the definition states (#698), through `tools/call`.

    With a session open and no flash_firmware call in this server, every
    backend answers from the session, so pyOCD and STM32CubeProgrammer do not
    ask for a flashed ELF. Once the session ends, OpenOCD refuses with
    session_not_active and the other two look for the ELF flash_firmware last
    wrote, of which there is none.
    """
    service, started = session_on(backend, tmp_path, monkeypatch)
    try:
        during = call(service, "boot_counter")
        assert service.call("debug_stop_session")["ok"] is True
        after = call(service, "boot_counter")
    finally:
        service.close()

    answer = during["structuredContent"]
    assert during["isError"] is False, answer
    assert answer["session"]["session_id"] == started["session"]["session_id"], answer
    assert "symbol_source" not in answer, answer
    refused = after["structuredContent"]
    assert after["isError"] is True, refused
    assert refused["error_type"] == WITHOUT_A_SESSION[backend], refused


@pytest.mark.parametrize("prepare", [stlink_flashed, pyocd_flashed], ids=["stlink-flashed-elf", "pyocd-flashed-elf"])
def test_a_resolution_without_a_session_contacts_no_target(tmp_path: Path, prepare: Callable[[Path], AgenticHILToolService]) -> None:
    service = prepare(tmp_path)
    try:
        answer = call(service, "boot_counter")["structuredContent"]
    finally:
        service.close()

    assert answer["ok"] is True, answer
    assert answer["target_contacted"] is False, answer
    assert answer["hardware_state"] == "unchanged", answer


# --- how it fails -------------------------------------------------------------


def openocd_allowlist_without_the_symbol(tmp_path: Path) -> tuple[AgenticHILToolService, str]:
    service = debug_service(tmp_path, allowed_symbols=["CTC_array"])
    assert start_debug_session(service)["ok"] is True
    return service, "test_done"


def openocd_without_a_session(tmp_path: Path) -> tuple[AgenticHILToolService, str]:
    return debug_service(tmp_path), "CTC_array"


def openocd_unknown_symbol(tmp_path: Path) -> tuple[AgenticHILToolService, str]:
    return openocd_session(tmp_path), "missing_symbol"


def stlink_nothing_flashed(tmp_path: Path) -> tuple[AgenticHILToolService, str]:
    return stlink_dump_service(tmp_path), "boot_counter"


def pyocd_nothing_flashed(tmp_path: Path) -> tuple[AgenticHILToolService, str]:
    return pyocd_read_service(tmp_path), "boot_counter"


def stlink_hex_flashed_after_the_elf(tmp_path: Path) -> tuple[AgenticHILToolService, str]:
    """A confirmed HEX flash puts an image with no symbols on the board and drops the ELF."""
    service = stlink_flashed(tmp_path)
    (tmp_path / "build" / "app.hex").write_text(OK_HEX, encoding="ascii")
    assert service.call("flash_firmware", {"image_path": "build/app.hex"})["ok"] is True
    return service, "boot_counter"


def stlink_elf_rebuilt_after_the_flash(tmp_path: Path) -> tuple[AgenticHILToolService, str]:
    service = stlink_flashed(tmp_path)
    (tmp_path / "build" / "app.elf").write_bytes(b"\x7fELF" + b"\x01" * 64)
    return service, "boot_counter"


def pyocd_elf_rebuilt_after_the_flash(tmp_path: Path) -> tuple[AgenticHILToolService, str]:
    service = pyocd_flashed(tmp_path)
    (tmp_path / "build" / "app.elf").write_bytes(b"\x7fELF" + b"\x01" * 64)
    return service, "boot_counter"


def stlink_elf_deleted_after_the_flash(tmp_path: Path) -> tuple[AgenticHILToolService, str]:
    service = stlink_flashed(tmp_path)
    (tmp_path / "build" / "app.elf").unlink()
    return service, "boot_counter"


@pytest.mark.parametrize(
    ("prepare", "error_type"),
    [
        (openocd_allowlist_without_the_symbol, "permission_denied"),
        (openocd_without_a_session, "session_not_active"),
        (openocd_unknown_symbol, "symbol_not_found"),
        (stlink_nothing_flashed, "symbol_source_not_available"),
        (pyocd_nothing_flashed, "symbol_source_not_available"),
        (stlink_hex_flashed_after_the_elf, "symbol_source_not_available"),
        (stlink_elf_rebuilt_after_the_flash, "symbol_source_changed"),
        (pyocd_elf_rebuilt_after_the_flash, "symbol_source_changed"),
        (stlink_elf_deleted_after_the_flash, "symbol_source_changed"),
    ],
    ids=[
        "not-allowed",
        "no-session",
        "unknown-symbol",
        "stlink-no-flashed-elf",
        "pyocd-no-flashed-elf",
        "stlink-hex-flashed-after-the-elf",
        "stlink-elf-rebuilt",
        "pyocd-elf-rebuilt",
        "stlink-elf-deleted",
    ],
)
def test_the_definition_explains_each_refusal_an_agent_acts_on(
    tmp_path: Path, listed: dict[str, dict], prepare: Callable[[Path], tuple[AgenticHILToolService, str]], error_type: str
) -> None:
    """The refusals that follow from the agent's own order of calls or its input.

    Not required in the text, because the result itself carries what to do:
    `invalid_argument` (the listed pattern refuses first), `gdb_not_found` and
    `debugger_not_executable` (bench setup, answered with the catalogue's
    remediation), and `timeout`, `symbol_ambiguous` and
    `symbol_resolution_failed` (GDB's own outcomes, with its message as the
    summary).
    """
    service, symbol = prepare(tmp_path / "bench")
    try:
        result = call(service, symbol)
    finally:
        service.close()

    answer = result["structuredContent"]
    assert result["isError"] is True, answer
    assert answer["error_type"] == error_type, answer
    assert explains(definition_text(listed), error_type), (error_type, definition_text(listed))


# --- the annotations stay true ------------------------------------------------


def test_the_annotations_still_describe_a_read_that_stays_on_this_bench(listed: dict[str, dict]) -> None:
    annotations = listed[TOOL]["annotations"]
    assert annotations["readOnlyHint"] is True
    assert annotations["openWorldHint"] is False
    assert annotations.get("destructiveHint") is not True
