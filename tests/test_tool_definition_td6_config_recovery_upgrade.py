"""What the configuration, recovery and upgrade tools tell an agent.

`project_config_describe`, `project_config_set`, `project_config_adopt_hardware`,
`project_config_reload_description`, `hardware_recover` and `server_upgrade`
named their purpose and little else: not which grant opens which half of the
file, which keys a re-read puts in force and which wait for a restart, which
holds refuse a write, a reload or an upgrade, what adoption leaves alone and
when its recovery may still reset the board, how long one read or one manager
run may take, which incidents a relayed statement may clear and what a failed
recovery leaves, or that an upgraded server keeps running the old release.
These tests ask the definitions a host receives through `tools/list` to say
those things, or, for a refusal a definition leaves to the error catalogue, the
catalogue entry `resources/read` serves for it. Every claim they ask for is
first shown to be what the code does, through `tools/call`, against stand-ins
for hardware discovery, the package manager and pyserial. No probe, port or
board is touched.

The metadata tests check meaning, not wording. A claim is checked as a
relation inside one sentence, clause or segment (the refusal and the hold that
produces it, the grant and the half of the file it opens, the default and what
it does), and the inverted claim an agent could act on wrongly is rejected
outright: a negation beside the claim, a default stated the other way, "both"
where either suffices, a deadline for the whole call where the bound is per
step. Every check is first run against a table of sentences it has to accept
and sentences it has to reject, so a check that cannot fail is caught there
rather than passing a definition that says the opposite.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from conftest import DEFAULT_TEST_PERMISSIONS
from test_agent_provisioning import bench as unprovisioned_bench
from test_bootstrap import _fixed_stlink
from test_config_adopt import _RecoveryBackend, _set_auto_recover, _timed_out_read, attached, placeholder_bench
from test_config_reload import add_second_board, rewrite
from test_config_reload import bench as reload_bench
from test_config_write import bench as write_bench
from test_config_write import changes, document_of
from test_config_write import service as open_service
from test_coordination import failing_write_record, restore_write_record
from test_mcp_reference_resources import read_text
from test_read_until import close, tools_call
from test_recover_tool import config_for, edit_config, ledger
from test_server_upgrade import (
    UV_EXACT_PIN_HINT,
    a_manager_that_cannot_be_reached,
    fake_manager,
    never_runs,
    upgradable_config,
)
from test_tool_definition_uart import (
    DESCRIPTION_LIMIT,
    NEGATION,
    PROPERTY_DESCRIPTION_LIMIT,
    QUOTED,
    SNAKE_CASE,
    answer_vocabulary,
    call,
    clauses,
    install_line,
    listed_tools,
    new_service,
    sentences,
)

from agentic_hil import __version__, upgrade
from agentic_hil.backends.common import CompletedCommand
from agentic_hil.config import ConfigError, config_schema, load_authoritative_config
from agentic_hil.configreload import RELOAD_IN_OPEN_RUN_ERROR
from agentic_hil.coordination import LEASE_RELEASE_RETRY_REASON, HardwareCoordinator, lease_config_sha256
from agentic_hil.knowledge import (
    CONFIG_DESCRIPTION_RIGHT,
    CONFIG_PERMISSIONS_RIGHT,
    ERROR_URI_PREFIX,
    RECOVERY_PHYSICAL_CHECK_ERROR,
    recovery_operator_command,
)
from agentic_hil.mcp import handle_mcp_message
from agentic_hil.tools import PROJECT_CONFIG_CREATE, AgenticHILToolService, UnprovisionedToolService

TD6_TOOLS = (
    "project_config_describe",
    "project_config_set",
    "project_config_adopt_hardware",
    "project_config_reload_description",
    "hardware_recover",
    "server_upgrade",
)
DESCRIBE, SET, ADOPT, RELOAD, RECOVER, UPGRADE = TD6_TOOLS
# Every input property, by the path `test_tool_descriptions` names it with.
PROPERTIES: dict[str, set[str]] = {
    DESCRIBE: set(),
    SET: {"/changes", "/changes/items/key", "/changes/items/value"},
    ADOPT: {"/apply", "/probe_id", "/debugger_id", "/com_port_id"},
    RELOAD: set(),
    RECOVER: {"/operator_statement", "/accept_config_change"},
    UPGRADE: set(),
}
# The sections a description re-read takes from disk.
RELOADED = ("target", "debuggers", "com_ports", "can_buses")

# Machine-wide device locks contend across sibling checkouts running the suite
# at the same time, so every probe, port and resource here has a name of its own.
HOLD_PROBE = "TD6-HOLD-0001"
HOLD_DEVICE = "/dev/ttyTD6HOLD0"
HOLD_PORTS = f'com_ports:\n  dut_uart:\n    device: "{HOLD_DEVICE}"\n    baudrate: 115200\n'
ADOPT_PROBE = "TD6-PROBE-0001"
OTHER_PROBE = "TD6-OTHER-0002"
CONFIGURED_PROBE = "TD6-CONF-0003"
ADOPT_PORT = "COMTD6"
RECOVER_RESOURCE = "physical:td6-recover-probe"


# ---------------------------------------------------------------------------
# What the definitions say, read the way a host reads them.


@pytest.fixture
def listed(tmp_path: Path) -> dict[str, dict]:
    return listed_tools(tmp_path / "listed")


def input_properties(node: object, where: str = "") -> Iterator[tuple[str, dict]]:
    """Every property of an input schema, at any depth, with its path.

    The path scheme is `test_tool_descriptions.property_descriptions`'s, so a
    path here names the same property the shared budget test names."""
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                if isinstance(child, dict):
                    yield f"{where}/{name}", child
        for key, child in node.items():
            yield from input_properties(child, f"{where}/{key}" if key != "properties" else where)
    elif isinstance(node, list):
        for child in node:
            yield from input_properties(child, where)


def description(tool: dict) -> str:
    return str(tool.get("description") or "")


def property_text(tool: dict, path: str) -> str:
    return str(dict(input_properties(tool["inputSchema"])).get(path, {}).get("description") or "")


def definition_text(tool: dict) -> str:
    """The description and every property description at any depth: what an agent reads."""
    return " ".join([description(tool), *(str(schema.get("description") or "") for _, schema in input_properties(tool["inputSchema"]))])


def segments(text: str) -> list[str]:
    """The clauses of `text`, each split once more at commas, colons and `and`."""
    return [piece for unit in clauses(text) for piece in re.split(r",\s*|:\s+|\s+and\s+", unit) if piece.strip()]


def prose(unit: str) -> str:
    """`unit` with its identifiers blanked, so a word inside `config_write_in_open_run` is not read as prose."""
    return SNAKE_CASE.sub(" ", unit)


def identifier(word: str) -> str:
    return rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])"


def mentions(unit: str, word: str) -> bool:
    return re.search(identifier(word), unit) is not None


def has(unit: str, pattern: str) -> bool:
    return re.search(pattern, unit, re.IGNORECASE) is not None


def unmet(text: str, *checks: Callable[[str], bool]) -> list[str]:
    """The names of the checks `text` does not satisfy."""
    return [check.__name__ for check in checks if not check(text)]


DEFAULT_FALSE = r"\bdefaults?\b[^.;]*\bfalse\b|\bfalse\b[^.;]*\bdefault\b"
DEFAULT_TRUE = r"\bdefaults?\b[^.;,]*\btrue\b|\btrue\b[^.;,]*\bdefault\b"
HOLD_NEGATION = r"\b(?:not|never|unless|except|without)\b"
RESTART = r"\brestart(?:s|ed|ing)?\b"
PERMISSION_WORD = r"\b(?:permissions?|grants?)\b"
GRANT_NAME = r"(?:permissions\.)?allow_config_(?:description|permissions)_write"
# Either kind of hold refuses, so the two are named as alternatives.
RUN_OR_SESSION = r"\b(?:an?\s+)?run\s+or\s+(?:an?\s+)?session\b|\b(?:an?\s+)?session\s+or\s+(?:an?\s+)?run\b"
HOLD_WORD = r"\b(?:holds?|holding|held)\b"


def states_default_false(text: str) -> bool:
    """A segment that says the default is false, with no negation before it, and
    no segment that pairs the default with true."""
    stated = False
    for unit in segments(text):
        found = re.search(DEFAULT_FALSE, unit, re.IGNORECASE)
        if found is not None and not has(unit[: found.start()], NEGATION) and not has(unit, r"\btrue\b"):
            stated = True
    return stated and not any(has(unit, DEFAULT_TRUE) for unit in segments(text))


def names_a_held_bench(unit: str) -> bool:
    """A run or a session, as alternatives, and a hold in one unit, with no exception to them."""
    words = prose(unit)
    return has(words, RUN_OR_SESSION) and has(words, HOLD_WORD) and not has(words, HOLD_NEGATION) and not has(words, r"\bboth\b")


def needs_the_grant(grant: str) -> Callable[[str], bool]:
    """A segment that says the call needs `grant`, with no negation beside it."""

    def check(text: str) -> bool:
        return any(mentions(unit, grant) and has(unit, r"\b(?:needs?|requires?)\b") and not has(prose(unit), NEGATION) for unit in segments(text))

    check.__name__ = f"needs_the_grant({grant})"
    return check


WHOLE_CALL = r"\b(?:whole|entire|total|overall|altogether)\b|\bin\s+all\b|\bthe\s+call\b"


def bounded_per_step(seconds: int) -> Callable[[str], bool]:
    """`seconds` named as the bound of each step, read or process, and nowhere as a
    deadline for the whole call."""

    def check(text: str) -> bool:
        bound = rf"\b{seconds}\s*(?:s|seconds?)\b"
        per_step = any(
            has(unit, bound) and has(unit, r"\b(?:each|every|per)\b") and has(prose(unit), r"\b(?:steps?|reads?|process(?:es)?|runs?)\b") and not has(prose(unit), WHOLE_CALL)
            for unit in clauses(text)
        )
        return per_step and not any(has(unit, bound) and has(prose(unit), WHOLE_CALL) for unit in clauses(text))

    check.__name__ = f"bounded_per_step({seconds})"
    return check


# project_config_describe


def nearest_before(unit: str, field: str, candidates: tuple[str, ...]) -> str | None:
    """Which of `candidates` is named last before `field` in `unit`."""
    found = re.search(identifier(field), unit)
    if found is None:
        return None
    named = re.findall("|".join(identifier(candidate) for candidate in candidates), unit[: found.start()])
    return named[-1] if named else None


KEY_LISTS = ("writable_keys", "locked_keys")


def lists_writable_and_locked_keys(text: str) -> bool:
    """writable_keys carries current_value and value_schema, locked_keys carries
    unlocked_by, and the clause says that what unlocked_by names is a permission."""
    return any(
        nearest_before(unit, "current_value", KEY_LISTS) == "writable_keys"
        and nearest_before(unit, "value_schema", KEY_LISTS) == "writable_keys"
        and nearest_before(unit, "unlocked_by", KEY_LISTS) == "locked_keys"
        and has(prose(unit), PERMISSION_WORD)
        and not has(prose(unit), NEGATION)
        for unit in clauses(text)
    )


def reads_before_setting(text: str) -> bool:
    return any(mentions(unit, "project_config_set") and has(unit, r"\bbefore\b") and not has(unit, r"\bafter\b") and not has(prose(unit), NEGATION) for unit in clauses(text))


NO_PERMISSION_NEEDED = r"\b(?:needs?|requires?)\s+no\s+(?:permission|grant)s?\b|\bno\s+(?:permission|grant)\s+is\s+(?:needed|required)\b|\bwithout\s+(?:any\s+)?(?:permission|grant)s?\b"


def needs_no_permission(text: str) -> bool:
    """Reading is free, said of the call itself and not of a write."""
    return any(has(prose(unit), NO_PERMISSION_NEEDED) and not has(prose(unit), r"\b(?:writ|chang)\w*") for unit in clauses(text))


def flags_a_held_bench(text: str) -> bool:
    return any(
        mentions(unit, "writes_blocked_by_open_run") and has(unit, r"\btrue\b") and not has(unit, r"\bfalse\b") and names_a_held_bench(unit)
        for unit in clauses(text)
    )


YAML_WORD = r"\b(?:yaml|pars(?:e|es|ed|ing)|syntax)\b"
UNREADABLE_WORD = r"\bunreadable\b|\bcan(?:not|'t)\s+be\s+read\b|\bnot\s+utf-?8\b|\bundecodable\b"


def tells_bad_yaml_from_unreadable(text: str) -> bool:
    """config_invalid beside a file that does not parse, config_unreadable beside
    one that cannot be read, and neither beside the other's cause."""
    invalid = [prose(unit) for unit in segments(text) if mentions(unit, "config_invalid")]
    unreadable = [prose(unit) for unit in segments(text) if mentions(unit, "config_unreadable")]
    return any(has(unit, YAML_WORD) and not has(unit, UNREADABLE_WORD) for unit in invalid) and any(
        has(unit, UNREADABLE_WORD) and not has(unit, YAML_WORD) for unit in unreadable
    )


# project_config_set


def grant_segments(text: str, grant: str) -> list[str]:
    """The segments naming `grant`, with every grant name blanked out of them."""
    return [prose(re.sub(GRANT_NAME, " ", unit)) for unit in segments(text) if mentions(unit, grant)]


def binds_each_grant_to_its_half(text: str) -> bool:
    """The description grant beside the device keys, the permissions grant beside
    the permission keys, neither beside the other half's word, and no negation."""
    device = r"\b(?:device|description)\b"
    permission = r"\bpermissions?\b"
    return any(has(unit, device) and not has(unit, permission) and not has(unit, NEGATION) for unit in grant_segments(text, CONFIG_DESCRIPTION_RIGHT)) and any(
        has(unit, permission) and not has(unit, device) and not has(unit, NEGATION) for unit in grant_segments(text, CONFIG_PERMISSIONS_RIGHT)
    )


def permission_keys_only_narrow(text: str) -> bool:
    return any(
        has(prose(unit), r"\bpermissions?\b") and has(unit, r"\bonly\b[^.;]*\bnarrow(?:s|ed|ing)?\b") and not has(unit, r"\bwiden") and not has(prose(unit), NEGATION)
        for unit in clauses(text)
    )


def names_the_refusal_without_a_grant(text: str) -> bool:
    return any(
        mentions(unit, "permission_denied") and re.search(GRANT_NAME, unit) is not None and has(prose(unit), r"\b(?:else|otherwise|without|lacking|missing)\b") and not has(prose(unit), r"\b(?:not|never)\b")
        for unit in clauses(text)
    )


def closing_a_config_grant_binds_at_once(text: str) -> bool:
    """Closing one of the two grants of this tool binds the next call, not the next restart."""
    return any(
        has(prose(unit), r"\bclos(?:e|es|ed|ing)\b")
        and (has(prose(unit), r"\b(?:either|each|both)\b") or re.search(GRANT_NAME, unit) is not None)
        and has(unit, r"\bat\s+once\b|\bimmediately\b|\bon\s+the\s+next\s+call\b")
        and not has(prose(unit), NEGATION)
        and not has(prose(unit), RESTART)
        for unit in segments(text)
    )


def freezes_permissions_when_the_grant_closes(text: str) -> bool:
    return any(
        mentions(unit, CONFIG_PERMISSIONS_RIGHT) and has(prose(unit), r"\bclos") and has(prose(unit), r"\bfreez") and mentions(unit, "permissions_frozen") and not has(prose(unit), NEGATION)
        for unit in segments(text)
    )


def not_in_force_until_reread(text: str) -> bool:
    """reload_required beside what it means: the loaded configuration stays until a re-read."""
    return any(
        mentions(unit, "reload_required")
        and has(unit, r"\buntil\b")
        and has(prose(unit), r"\bnot\s+in\s+force\b|\b(?:keeps?|still)\b")
        and (has(prose(unit), r"\bre-?reads?\b|\breloads?\b") or mentions(unit, RELOAD))
        and not has(unit, r"\b(?:immediately|at once|right away)\b")
        for unit in clauses(text)
    )


BLANKET = r"\b(?:all|every|any)\s+(?:\w+\s+)?(?:changes?|keys?|values?)\b[^.;]*\b(?:takes?\s+effect|in\s+force)\b"


def reload_scope_is_named(text: str) -> bool:
    """One sentence names the reload tool, the four sections it takes before a
    negation, and debug and permissions after it; no blanket claim that every
    change takes effect."""
    if has(text, BLANKET):
        return False
    for unit in sentences(text):
        found = re.search(identifier("debug"), unit)
        if not mentions(unit, RELOAD) or found is None:
            continue
        cuts = [match.start() for match in re.finditer(r"\b(?:not|never|except)\b", unit[: found.start()], re.IGNORECASE)]
        if not cuts:
            continue
        cut = cuts[-1]
        listed = [re.search(identifier(section), unit) for section in RELOADED]
        if all(match is not None and match.start() < cut for match in listed) and has(unit[cut:], r"\bpermissions\b"):
            return True
    return False


NOTHING_WRITTEN = r"\bnothing\b[^.;]*\b(?:written|writes|changed)\b|\bno\s+(?:key|change|value)s?\b[^.;]*\b(?:written|changed)\b"


def changes_are_all_or_nothing(text: str) -> bool:
    return has(text, r"\bone or more\b|\bat least one\b") and any(
        mentions(unit, "invalid_argument") and has(unit, r"\bunknown\b") and has(unit, r"\brepeated\b|\bduplicated?\b|\bat most once\b") and has(prose(unit), NOTHING_WRITTEN)
        for unit in sentences(text)
    )


def key_is_listed_by_describe(text: str) -> bool:
    return any(mentions(unit, "project_config_describe") and has(unit, r"\blists?\b") and not has(prose(unit), NEGATION) for unit in clauses(text))


def value_is_one_scalar_matching_its_schema(text: str) -> bool:
    """A scalar that has to match the key's value_schema, and objects and arrays refused."""
    scalar = any(
        has(unit, r"\bscalar\b") and mentions(unit, "value_schema") and has(unit, r"\bmatch(?:es|ing)?\b") and not has(prose(unit), NEGATION) and not has(unit, r"\bany\b")
        for unit in clauses(text)
    )
    refused = any(has(unit, r"\bobjects?\b") and has(unit, r"\barrays?\b") and has(unit, r"\b(?:refused|rejected)\b") and not has(prose(unit), NEGATION) for unit in clauses(text))
    return scalar and refused


def permission_keys_take_only_false(text: str) -> bool:
    only_false = any(has(unit, r"\bpermission\b") and has(unit, r"\bonly\s+false\b") and not has(unit, r"\btrue\b") and not has(prose(unit), NEGATION) for unit in segments(text))
    true_refused = any(has(unit, r"\btrue\b") and mentions(unit, "permission_widening_denied") and not has(prose(unit), NEGATION) for unit in segments(text))
    return only_false and true_refused


# project_config_adopt_hardware

NEGATED_FLASH_ERASE = r"\b(?:no|never|not|without)\s+(?:\w+\s+)?flash\w*(?:,\s*|\s+(?:or|nor)\s+)eras\w*"
RESET_DENIED = r"\b(?:no|never|not|without|nor)\b[^.;]*\breset"


def no_flash_or_erase_and_no_reset_claim(text: str) -> bool:
    """Reading flashes and erases nothing, and nothing claims it never resets: the
    recovery after a read that left the board in an unknown state may."""
    return any(has(unit, NEGATED_FLASH_ERASE) for unit in clauses(text)) and not any(has(unit, RESET_DENIED) for unit in clauses(text))


def unknown_state_may_reset_into_halt(text: str) -> bool:
    return any(
        mentions(unit, "resource_quarantined")
        and has(prose(unit), r"\bunknown\b")
        and has(unit, r"\b(?:may|can|might)\b[^.;]*\breset")
        and has(unit, r"\bhalt(?:ed)?\b")
        and not has(prose(unit), r"\b(?:never|not|no)\b")
        for unit in clauses(text)
    )


def fills_only_placeholders_from_what_is_attached(text: str) -> bool:
    """What adoption fills is the keys still holding a placeholder, with what the
    attached hardware answers; never every key."""
    return any(
        has(unit, r"\bfill(?:s|ed|ing)?\b")
        and has(unit, r"\bplaceholders?\b")
        and has(prose(unit), r"\battached\b")
        and not has(unit, r"\b(?:every|all|any)\s+(?:\w+\s+)?keys?\b")
        and not has(prose(unit), NEGATION)
        and not has(prose(unit), HOLD_NEGATION)
        for unit in clauses(text)
    )


WITHOUT_APPLY = r"\bwrites?\s+nothing\b[^.;]*\b(?:unless|without|until)\b[^.;]*\bapply\b|\bwrites?\s+only\s+(?:with|when|if)\b[^.;]*\bapply\b"


def writes_nothing_without_apply(text: str) -> bool:
    return any(has(unit, WITHOUT_APPLY) and has(unit, r"\bapply\b[^.;]*\btrue\b") and not has(unit, r"\bfalse\b") for unit in clauses(text))


def apply_writes_through_project_config_set(text: str) -> bool:
    plan_by_default = any(has(unit, DEFAULT_FALSE) and has(unit, r"\bwrites?\s+nothing\b|\bchanges?\s+nothing\b") for unit in sentences(text))
    written_through_set = any(
        has(unit, r"\btrue\b")
        and has(unit, r"\bwrites?\b")
        and mentions(unit, "project_config_set")
        and has(unit, r"\b(?:through|via)\b")
        and mentions(unit, "allow_config_description_write")
        and mentions(unit, "permission_denied")
        for unit in clauses(text)
    )
    return plan_by_default and written_through_set


def tells_to_reload_after_writing(text: str) -> bool:
    return any(
        mentions(unit, "project_config_reload_description") and has(unit, r"\b(?:then|after|afterwards|next)\b") and not has(prose(unit), NEGATION) and not has(prose(unit), RESTART)
        for unit in clauses(text)
    )


def nothing_carried_answers_applied_false(text: str) -> bool:
    """A second apply over a file that already holds every value writes nothing and says so."""
    return any(has(prose(unit), r"\b(?:none|nothing)\b") and has(unit, r"\bapplied\b[\s:]*false\b") and not has(unit, r"\btrue\b") for unit in clauses(text))


def refuses_another_board(text: str) -> bool:
    return any(
        mentions(unit, "hardware_mismatch") and has(prose(unit), r"\b(?:another|other|different)\b") and has(prose(unit), r"\b(?:probe|board|serial)\b") and not has(prose(unit), NEGATION)
        for unit in clauses(text)
    )


def probe_id_defaults_and_disambiguates(text: str) -> bool:
    defaults = any(has(unit, r"\bdefaults?\b") and has(unit, r"\bconfigured\b") and mentions(unit, "probe_id") for unit in segments(text))
    ambiguous = any(mentions(unit, "ambiguous_hardware") and has(unit, r"\b(?:several|multiple|more than one)\b") for unit in clauses(text))
    never_adds = any(has(unit, r"\bselects?\b") and has(unit, r"\bnever\s+adds?\b|\bdoes\s+not\s+add\b") for unit in clauses(text))
    return defaults and ambiguous and never_adds


def debugger_id_names_a_configured_entry(text: str) -> bool:
    configured = any(has(unit, r"\bconfigured\b") and has(unit, r"\b(?:debuggers|entry)\b") for unit in segments(text))
    several = any(mentions(unit, "invalid_argument") and has(unit, r"\b(?:several|multiple|more than one)\b") and not has(prose(unit), NEGATION) for unit in clauses(text))
    unknown = any(mentions(unit, "unknown_device") and has(unit, r"\bnot\s+configured\b") for unit in sentences(text))
    return configured and several and unknown


COM_PORT_CHAIN = (r"\bdefaults?\b", r"\bnam(?:es|ing)\b", r"\bonly\s+one\b", r"\bnew\b")


def com_port_default_chain(text: str) -> bool:
    """The default, in the order the code tries it: the entry naming the device,
    then the only entry, then a new one."""
    for unit in sentences(text):
        steps = [re.search(pattern, unit, re.IGNORECASE) for pattern in COM_PORT_CHAIN]
        if all(step is not None for step in steps) and not has(prose(unit), NEGATION):
            starts = [step.start() for step in steps if step is not None]
            if starts == sorted(starts):
                return True
    return False


def created_with_every_permission_false(text: str) -> bool:
    return any(has(unit, r"\bcreated\b") and has(unit, r"\bevery\s+permission\s+false\b") and not has(prose(unit), NEGATION) for unit in clauses(text))


def several_unnamed_ports_answer_unavailable(text: str) -> bool:
    return any(mentions(unit, "unavailable") and has(unit, r"\b(?:several|multiple|more than one)\b") for unit in sentences(text))


# project_config_reload_description


def rereads_the_four_sections(text: str) -> bool:
    """The four sections named after the re-read and before any negation in the clause."""
    for unit in clauses(text):
        if not has(unit, r"\bre-?reads?\b|\breloads?\b"):
            continue
        negation = re.search(NEGATION, unit, re.IGNORECASE)
        end = negation.start() if negation is not None else len(unit)
        found = [re.search(identifier(section), unit) for section in RELOADED]
        if all(match is not None and match.start() < end for match in found):
            return True
    return False


def never_takes_permissions_or_debug(text: str) -> bool:
    return any(has(unit, r"\b(?:never|not|except)\s+(?:the\s+)?permissions\b") and mentions(unit, "debug") for unit in segments(text))


def changed_permissions_need_a_restart(text: str) -> bool:
    named = any(mentions(unit, "restart_required_for") and has(prose(unit), PERMISSION_WORD) and has(unit, r"\bchang") for unit in sentences(text))
    restart = any(has(prose(unit), RESTART) and has(prose(unit), PERMISSION_WORD) and has(unit, r"\bchang") and not has(prose(unit), NEGATION) for unit in segments(text))
    return named and restart


def new_devices_arrive_granted_nothing(text: str) -> bool:
    return any(
        has(unit, r"\bnew\b")
        and has(unit, r"\b(?:devices?|entr(?:y|ies)|boards?)\b")
        and has(unit, r"\ball\s+false\b|\bevery\s+permission\s+false\b|\bno\s+(?:grant|permission)s?\b")
        and not has(unit, r"\btrue\b")
        for unit in segments(text)
    )


def follows_a_write_instead_of_a_restart(text: str) -> bool:
    return any(
        mentions(unit, "project_config_set") and mentions(unit, "project_config_adopt_hardware") and has(unit, r"\bafter\b") and has(prose(unit), r"\b(?:instead of|rather than)\b[^.;]*" + RESTART)
        for unit in clauses(text)
    )


def reports_what_changed(text: str) -> bool:
    return any(
        mentions(unit, "description_changes") and has(prose(unit), r"\b(?:lists?|names?|reports?)\b") and has(prose(unit), r"\b(?:moved|changed)\b") and not has(prose(unit), NEGATION)
        for unit in clauses(text)
    )


# hardware_recover

NO_CONTACT = r"\bno[\s-]+(?:hardware[\s-]+)?contact\b"


def clears_the_standing_quarantine(text: str) -> bool:
    return any(has(unit, r"\bclears?\b") and has(unit, r"\b(?:quarantine|incident)\b") and has(unit, r"\bstand(?:s|ing)\b") and not has(prose(unit), r"\b(?:not|never)\b") for unit in clauses(text))


def touches_no_hardware(text: str) -> bool:
    return any(
        has(unit, r"\btouch(?:es)?\s+no\s+hardware\b|\bnever\s+touches\s+(?:the\s+)?(?:hardware|board)\b|\bno\s+hardware\s+is\s+touched\b|\bdoes\s+not\s+touch\s+(?:the\s+)?(?:hardware|board)\b")
        for unit in segments(text)
    )


def called_when_the_lease_status_shows_an_incident(text: str) -> bool:
    """The moment to call it is the one hardware_lease_status reports incident_stands."""
    return any(
        mentions(unit, "hardware_lease_status")
        and mentions(unit, "incident_stands")
        and has(unit, r"\b(?:when|once|if)\b")
        and not has(unit, r"\bfalse\b")
        and not has(prose(unit), NEGATION)
        and not has(prose(unit), HOLD_NEGATION)
        for unit in clauses(text)
    )


def no_contact_clears_alone_and_physical_needs_a_statement(text: str) -> bool:
    alone = any(has(unit, NO_CONTACT) and has(unit, r"\b(?:no|without\s+an?)\s+argument\b") and not mentions(unit, RECOVERY_PHYSICAL_CHECK_ERROR) for unit in clauses(text))
    statement = any(
        mentions(unit, RECOVERY_PHYSICAL_CHECK_ERROR)
        and mentions(unit, "operator_statement")
        and has(prose(unit), r"\bask\b[^.;]*\boperator\b")
        and has(unit, r"\bverbatim\b")
        and not has(unit, NO_CONTACT)
        for unit in clauses(text)
    )
    return alone and statement


def audit_broken_clears_only_by_the_operator_command(text: str) -> bool:
    return any(
        mentions(unit, "audit_broken")
        and has(prose(unit), r"\bno\s+statement\b|\bnot\s+(?:by\s+)?(?:a\s+)?statement\b|\bstatement\b[^.;]*\bnever\b")
        and mentions(unit, "operator_command")
        and has(prose(unit), r"\bonly\b")
        for unit in sentences(text)
    )


def operator_statement_is_the_operators_words(text: str) -> bool:
    relayed = any(has(prose(unit), r"\boperator\b") and has(unit, r"\bverbatim\b") and has(unit, r"\bchat\b") for unit in clauses(text))
    recorded = any(has(unit, r"\bledger\b") and has(unit, r"\brecorded\b") for unit in clauses(text))
    never_invented = has(text, r"\bnever\s+invent")
    return relayed and recorded and never_invented


def accepts_a_reviewed_config_change(text: str) -> bool:
    return (
        has(text, DEFAULT_FALSE)
        and any(has(unit, r"\bonly\s+after\b") and has(unit, r"\boperator\b") and has(unit, r"\bdigests?\b") and mentions(unit, "config_changed") for unit in clauses(text))
        and has(text, r"\bledger\b")
    )


# server_upgrade


def upgrades_with_the_manager_that_installed_it(text: str) -> bool:
    return any(
        all(has(unit, rf"\b{manager}\b") for manager in ("uv", "pipx", "pip"))
        and has(unit, r"\b(?:that|which)\s+installed\s+(?:it|this\s+(?:copy|installation))\b")
        and has(unit, r"\b(?:newest|latest)\s+release\b")
        and not has(prose(unit), NEGATION)
        for unit in clauses(text)
    )


def takes_no_arguments(text: str) -> bool:
    return any(has(unit, r"\bno\s+arguments?\b") and not has(unit, r"\b(?:except|but|other\s+than)\b") for unit in segments(text))


def windows_is_the_operators_command(text: str) -> bool:
    return any(has(unit, r"\bWindows\b") and mentions(unit, "upgrade_cli_only_on_host") and has(unit, r"\boperator\b") and "agentic-hil upgrade" in unit for unit in sentences(text))


def keeps_running_the_old_release_until_restart(text: str) -> bool:
    return any(
        mentions(unit, "running_version")
        and has(prose(unit), r"\b(?:old|previous|unchanged|the\s+same)\b")
        and has(prose(unit), r"\b(?:stays?|remains?|keeps?)\b")
        and has(unit, r"\buntil\b")
        and has(prose(unit), RESTART)
        and not has(prose(unit), NEGATION)
        for unit in clauses(text)
    )


def failure_hands_reinstall_command_to_the_operator(text: str) -> bool:
    return any(
        has(prose(unit), r"\bfail(?:s|ed|ure)?\b") and mentions(unit, "reinstall_command") and has(prose(unit), r"\boperator\b") and has(prose(unit), r"\bruns?\b") and not has(prose(unit), NEGATION)
        for unit in clauses(text)
    )


# The refusals a definition leaves to the error catalogue, as its entries say them.

RUN_AND_SESSION_TOGETHER = r"\bboth\b|\btogether\b|\bas\s+well\s+as\b|\bruns?\s+and\b"


def arises_while_a_run_or_session_is_held(text: str) -> bool:
    """A sentence that puts the refusal while this server holds something, with a
    run and a session among the alternatives that count as a hold."""
    return any(
        has(unit, r"\bwhile\b[^.;]*" + HOLD_WORD)
        and has(unit, r"\bruns?\b[^.;]*\bor\b[^.;]*\bsessions?\b|\bsessions?\b[^.;]*\bor\b[^.;]*\bruns?\b")
        and not has(unit, RUN_AND_SESSION_TOGETHER)
        and not has(unit, NEGATION)
        and not has(unit, HOLD_NEGATION)
        for unit in map(prose, sentences(text))
    )


def held_by_an_incident(text: str) -> bool:
    """The resource is refused because an incident holds it."""
    return any(
        has(unit, r"\bincidents?\b") and has(unit, r"\bheld\s+by\b|\bholds?\b|\bstands?\b") and not has(unit, NEGATION) and not has(unit, HOLD_NEGATION)
        for unit in map(prose, segments(text))
    )


def the_quarantine_still_stands(text: str) -> bool:
    """A failed recovery leaves the quarantine standing, not stood down."""
    return any(
        has(unit, r"\b(?:quarantine|incident)\b")
        and has(unit, r"\bstands\b")
        and not has(unit, r"\bstands?\b[^.;]*\bdown\b")
        and not has(unit, NEGATION)
        and not has(unit, HOLD_NEGATION)
        for unit in map(prose, segments(text))
    )


def a_rerun_resumes(text: str) -> bool:
    """Running the recovery again picks up where the failed one stopped, said in
    prose: a quoted `resumed` is the ledger's word, not the claim."""
    return any(
        has(unit, r"\bresum") and has(unit, r"\b(?:retry|retried|again|rerun)\b") and not has(unit, NEGATION) and not has(unit, HOLD_NEGATION)
        for unit in (prose(re.sub(r"`[^`]*`", " ", sentence)) for sentence in sentences(text))
    )


# ---------------------------------------------------------------------------
# Each check against sentences it has to accept and reject.

CONTROLS: list[tuple[Callable[[str], bool], str, bool]] = [
    (states_default_false, "Default false. True only after the operator has reviewed both digests.", True),
    (states_default_false, "Default false writes nothing. true writes carried.", True),
    (states_default_false, "Never defaults to false, true is the default.", False),
    (states_default_false, "Default true.", False),
    (states_default_false, "Defaults to true, false writes nothing.", False),
    (bounded_per_step(10), "Each read step may take 10 s.", True),
    (bounded_per_step(10), "The call finishes within 10 s.", False),
    (bounded_per_step(10), "Each read step may take 10 s, the whole call at most 10 s.", False),
    (bounded_per_step(10), "Serial of the attached probe to read, each read step taking up to 10 s; defaults to the configured probe_id.", True),
    (bounded_per_step(10), "Serial of the attached probe to read, the whole call taking up to 10 s.", False),
    (bounded_per_step(600), "Each manager run may take 600 s.", True),
    (bounded_per_step(600), "The upgrade takes 600 s in all.", False),
    (lists_writable_and_locked_keys, "List the keys: writable_keys, each with current_value and value_schema, and locked_keys, each naming in unlocked_by the permission that opens it.", True),
    (lists_writable_and_locked_keys, "writable_keys carries current_value and value_schema, locked_keys the grant in unlocked_by.", True),
    (lists_writable_and_locked_keys, "writable_keys, each naming in unlocked_by the permission that opens it, and locked_keys, each with current_value and value_schema.", False),
    (lists_writable_and_locked_keys, "writable_keys, each with current_value and value_schema, and locked_keys, each naming unlocked_by.", False),
    (lists_writable_and_locked_keys, "writable_keys never carries current_value or value_schema, locked_keys the grant in unlocked_by.", False),
    (lists_writable_and_locked_keys, "List the configuration keys you may change and the permission that opens each locked one.", False),
    (reads_before_setting, "Read it before project_config_set instead of guessing.", True),
    (reads_before_setting, "Read it after project_config_set.", False),
    (reads_before_setting, "Do not read it before project_config_set.", False),
    (needs_no_permission, "Read it first; it needs no permission.", True),
    (needs_no_permission, "Reading it requires no grant.", True),
    (needs_no_permission, "Writing needs no permission.", False),
    (needs_no_permission, "Changing a key requires no grant.", False),
    (needs_no_permission, "It needs permissions.allow_config_description_write.", False),
    (needs_no_permission, "It does not show the permission it needs.", False),
    (flags_a_held_bench, "writes_blocked_by_open_run is true while this server holds a run or session.", True),
    (flags_a_held_bench, "writes_blocked_by_open_run is true while a run or session is held.", True),
    (flags_a_held_bench, "writes_blocked_by_open_run is false while this server holds a run or session.", False),
    (flags_a_held_bench, "writes_blocked_by_open_run is true unless this server holds a run or session.", False),
    (flags_a_held_bench, "writes_blocked_by_open_run is true while this server holds a run.", False),
    (flags_a_held_bench, "writes_blocked_by_open_run is true while this server holds both a run and a session.", False),
    (tells_bad_yaml_from_unreadable, "Bad YAML answers config_invalid, an unreadable file config_unreadable.", True),
    (tells_bad_yaml_from_unreadable, "A file that does not parse answers config_invalid; one that is not UTF-8 answers config_unreadable.", True),
    (tells_bad_yaml_from_unreadable, "An unreadable configuration file answers config_invalid.", False),
    (tells_bad_yaml_from_unreadable, "Bad YAML answers config_unreadable, an unreadable file config_invalid.", False),
    (tells_bad_yaml_from_unreadable, "A missing file answers config_invalid, an unreadable one config_unreadable.", False),
    (binds_each_grant_to_its_half, "Device keys need permissions.allow_config_description_write, permission keys allow_config_permissions_write.", True),
    (binds_each_grant_to_its_half, "allow_config_description_write gates the device description, allow_config_permissions_write the permissions.", True),
    (binds_each_grant_to_its_half, "Device keys need allow_config_permissions_write, permission keys allow_config_description_write.", False),
    (binds_each_grant_to_its_half, "allow_config_description_write and allow_config_permissions_write gate the device keys and the permissions.", False),
    (binds_each_grant_to_its_half, "Device keys never need allow_config_description_write, permission keys never need allow_config_permissions_write.", False),
    (permission_keys_only_narrow, "Permission keys need allow_config_permissions_write and can only be narrowed.", True),
    (permission_keys_only_narrow, "Permissions only narrow: closing allow_config_permissions_write freezes them all at once (permissions_frozen).", True),
    (permission_keys_only_narrow, "Permissions can be widened, not only narrowed: closing allow_config_permissions_write freezes them all at once.", False),
    (permission_keys_only_narrow, "Permission keys can be widened and narrowed.", False),
    (permission_keys_only_narrow, "Permission keys can only be widened.", False),
    (permission_keys_only_narrow, "Permission keys are not only narrowed.", False),
    (names_the_refusal_without_a_grant, "Device keys need allow_config_description_write, else permission_denied.", True),
    (names_the_refusal_without_a_grant, "Without allow_config_description_write it answers permission_denied.", True),
    (names_the_refusal_without_a_grant, "permission_denied never comes from a missing allow_config_description_write.", False),
    (names_the_refusal_without_a_grant, "allow_config_description_write gates the device description.", False),
    (closing_a_config_grant_binds_at_once, "Device keys need allow_config_description_write, permission keys allow_config_permissions_write; closing either binds at once.", True),
    (closing_a_config_grant_binds_at_once, "Closing allow_config_description_write binds on the next call.", True),
    (closing_a_config_grant_binds_at_once, "Permissions only narrow: closing allow_config_permissions_write freezes them all at once (permissions_frozen).", True),
    (closing_a_config_grant_binds_at_once, "Permissions only narrow: closing allow_config_permissions_write freezes them all after a restart.", False),
    (closing_a_config_grant_binds_at_once, "Closing either binds after a restart.", False),
    (closing_a_config_grant_binds_at_once, "Closing either does not bind at once.", False),
    (freezes_permissions_when_the_grant_closes, "closing allow_config_permissions_write freezes every permission (permissions_frozen).", True),
    (freezes_permissions_when_the_grant_closes, "closing allow_config_permissions_write never freezes a permission (permissions_frozen).", False),
    (freezes_permissions_when_the_grant_closes, "closing allow_config_description_write freezes every permission (permissions_frozen).", False),
    (not_in_force_until_reread, "This server keeps its loaded configuration until it re-reads the file (reload_required).", True),
    (not_in_force_until_reread, "Until project_config_reload_description runs this server still answers out of the old description (reload_required).", True),
    (not_in_force_until_reread, "Not in force until re-read (reload_required).", True),
    (not_in_force_until_reread, "The change is in force at once (reload_required).", False),
    (not_in_force_until_reread, "This server keeps its loaded configuration until a restart (reload_required).", False),
    (not_in_force_until_reread, "This server reloads immediately and keeps it until the next write (reload_required).", False),
    (
        reload_scope_is_named,
        "Not in force until re-read (reload_required): project_config_reload_description takes target, debuggers, com_ports, can_buses keys, not debug or permissions.",
        True,
    ),
    (reload_scope_is_named, "Every change takes effect after project_config_reload_description.", False),
    (reload_scope_is_named, "project_config_reload_description takes target, debuggers, com_ports, can_buses, debug and permissions keys.", False),
    (reload_scope_is_named, "project_config_reload_description takes debug and permissions keys, not target, debuggers, com_ports or can_buses.", False),
    (
        reload_scope_is_named,
        "project_config_reload_description takes target, debuggers, com_ports, can_buses keys, not debug or permissions; every key change is in force after it.",
        False,
    ),
    (changes_are_all_or_nothing, "One or more pairs, each key at most once: an unknown or repeated key answers invalid_argument and nothing is written.", True),
    (changes_are_all_or_nothing, "One or more pairs: an unknown or repeated key answers invalid_argument and the others are written.", False),
    (changes_are_all_or_nothing, "Pairs: an unknown or repeated key answers invalid_argument and nothing is written.", False),
    (key_is_listed_by_describe, "Dotted configuration key. project_config_describe lists the ones this caller may set.", True),
    (key_is_listed_by_describe, "Call it after project_config_describe lists the keys, rather than editing the configuration file.", True),
    (key_is_listed_by_describe, "Call it without project_config_describe listing the keys.", False),
    (key_is_listed_by_describe, "Dotted configuration key.", False),
    (key_is_listed_by_describe, "Dotted key project_config_describe never lists.", False),
    (value_is_one_scalar_matching_its_schema, "A single scalar matching the key's value_schema; objects and arrays are refused.", True),
    (value_is_one_scalar_matching_its_schema, "A scalar, an object or an array.", False),
    (value_is_one_scalar_matching_its_schema, "No scalar matching value_schema is allowed; objects and arrays are never refused.", False),
    (value_is_one_scalar_matching_its_schema, "Any scalar value is accepted, whatever value_schema matches; objects and arrays are refused.", False),
    (value_is_one_scalar_matching_its_schema, "A single scalar; objects and arrays are refused.", False),
    (permission_keys_take_only_false, "A permission key takes only false: true answers permission_widening_denied.", True),
    (permission_keys_take_only_false, "A permission key takes only true: false answers permission_widening_denied.", False),
    (permission_keys_take_only_false, "A permission key takes only false: true never answers permission_widening_denied.", False),
    (permission_keys_take_only_false, "A permission key does not take only false: true answers permission_widening_denied.", False),
    (no_flash_or_erase_and_no_reset_claim, "Read the attached probe (no flash or erase).", True),
    (no_flash_or_erase_and_no_reset_claim, "No flash or erase; recovery may reset it into halt.", True),
    (no_flash_or_erase_and_no_reset_claim, "No flash, erase or reset.", False),
    (no_flash_or_erase_and_no_reset_claim, "It never flashes, erases or resets the board.", False),
    (no_flash_or_erase_and_no_reset_claim, "It flashes, erases or resets the board.", False),
    (no_flash_or_erase_and_no_reset_claim, "No flash, but erases.", False),
    (unknown_state_may_reset_into_halt, "One leaving the board state unknown answers resource_quarantined and recovery may reset it into halt.", True),
    (unknown_state_may_reset_into_halt, "One leaving the board state unknown answers resource_quarantined and it is never reset into halt.", False),
    (unknown_state_may_reset_into_halt, "An unknown state answers resource_quarantined; recovery may reset it into halt.", False),
    (fills_only_placeholders_from_what_is_attached, "Fill the configuration keys that still hold placeholders with what hardware discovery finds for the attached probe.", True),
    (fills_only_placeholders_from_what_is_attached, "Fill every configuration key, placeholder or set, with what the attached probe reports.", False),
    (fills_only_placeholders_from_what_is_attached, "Fill the placeholders with values you type, never from the attached probe.", False),
    (fills_only_placeholders_from_what_is_attached, "Overwrite the keys somebody set with what the attached probe reports.", False),
    (writes_nothing_without_apply, "Writes nothing unless apply is true.", True),
    (writes_nothing_without_apply, "Fills placeholders; writes only with apply: true.", True),
    (writes_nothing_without_apply, "Writes nothing unless apply is false.", False),
    (writes_nothing_without_apply, "Writes unless apply is false.", False),
    (apply_writes_through_project_config_set, "Default false: write nothing. true writes carried through project_config_set (needs allow_config_description_write, else permission_denied).", True),
    (apply_writes_through_project_config_set, "Default true: write the plan. true writes carried through project_config_set (needs allow_config_description_write, else permission_denied).", False),
    (apply_writes_through_project_config_set, "Default false: write nothing. true writes carried straight to the file (needs allow_config_description_write, else permission_denied).", False),
    (tells_to_reload_after_writing, "true writes carried; then project_config_reload_description.", True),
    (tells_to_reload_after_writing, "Do not call project_config_reload_description after writing.", False),
    (tells_to_reload_after_writing, "Then restart the server rather than calling project_config_reload_description.", False),
    (nothing_carried_answers_applied_false, "true writes carried; none: applied false.", True),
    (nothing_carried_answers_applied_false, "true writes carried; none: applied true.", False),
    (nothing_carried_answers_applied_false, "true writes carried; applied false.", False),
    (refuses_another_board, "hardware_mismatch if the entry names another probe.", True),
    (refuses_another_board, "hardware_mismatch never comes from another probe.", False),
    (refuses_another_board, "It may answer hardware_mismatch.", False),
    (
        probe_id_defaults_and_disambiguates,
        "Serial of the probe; defaults to the entry's configured probe_id. Without either, several attached probes answer ambiguous_hardware. Selects among attached probes, never adds one.",
        True,
    ),
    (
        probe_id_defaults_and_disambiguates,
        "Serial of the probe; defaults to the first attached probe_id. Without either, several attached probes answer ambiguous_hardware. Selects among attached probes, never adds one.",
        False,
    ),
    (probe_id_defaults_and_disambiguates, "Which attached probe this is about. Needed when more than one is attached; it selects among the attached probes and never adds one.", False),
    (
        probe_id_defaults_and_disambiguates,
        "Serial of the attached probe to read; defaults to the configured probe_id. With neither, several attached probes answer ambiguous_hardware. Selects, never adds one.",
        True,
    ),
    (
        probe_id_defaults_and_disambiguates,
        "Serial of the attached probe to read; defaults to the configured probe_id. With neither, several attached probes answer ambiguous_hardware. Selects, or adds one.",
        False,
    ),
    (
        debugger_id_names_a_configured_entry,
        "Configured debuggers entry that receives the values; needed when there are several (else invalid_argument). A name not configured answers unknown_device.",
        True,
    ),
    (debugger_id_names_a_configured_entry, "Configured debuggers entry; needed when there are several (else invalid_argument). A new name adds an entry.", False),
    (debugger_id_names_a_configured_entry, "Which configured debugger entry receives the values. Only needed when the configuration declares more than one.", False),
    (com_port_default_chain, "Default: the entry naming the device, else the only one, else a new one.", True),
    (com_port_default_chain, "Default: a new one, else the entry naming the device, else the only one.", False),
    (com_port_default_chain, "Which com_ports entry receives the device.", False),
    (created_with_every_permission_false, "Else a new dut_uart, created with every permission false.", True),
    (created_with_every_permission_false, "Created with the permissions the file grants.", False),
    (created_with_every_permission_false, "No port is created with every permission false.", False),
    (several_unnamed_ports_answer_unavailable, "Several, none named: unavailable.", True),
    (several_unnamed_ports_answer_unavailable, "None named: unavailable.", False),
    (rereads_the_four_sections, "Re-read target, debuggers, com_ports and can_buses.", True),
    (rereads_the_four_sections, "Re-read target and debuggers.", False),
    (rereads_the_four_sections, "Never re-read target, debuggers, com_ports and can_buses.", False),
    (rereads_the_four_sections, "Re-read target and debuggers, never com_ports or can_buses.", False),
    (never_takes_permissions_or_debug, "Never permissions or debug: new devices get all false.", True),
    (never_takes_permissions_or_debug, "Re-read target, debuggers, com_ports, can_buses (never permissions or debug) from the file.", True),
    (never_takes_permissions_or_debug, "Never permissions: new devices get all false.", False),
    (never_takes_permissions_or_debug, "Re-read target, debuggers, debug and permissions.", False),
    (never_takes_permissions_or_debug, "It never skips permissions or debug.", False),
    (changed_permissions_need_a_restart, "Never permissions: changed grants need a restart (restart_required_for).", True),
    (changed_permissions_need_a_restart, "Never permissions: changed grants need no restart (restart_required_for).", False),
    (changed_permissions_need_a_restart, "Changed grants are taken at once (restart_required_for).", False),
    (new_devices_arrive_granted_nothing, "Never permissions: new devices get all false.", True),
    (new_devices_arrive_granted_nothing, "New devices get the grants the file gives them.", False),
    (new_devices_arrive_granted_nothing, "New devices get all true.", False),
    (follows_a_write_instead_of_a_restart, "Re-read the file after project_config_set, project_config_adopt_hardware or an edit instead of a restart.", True),
    (follows_a_write_instead_of_a_restart, "Re-read the file after a board was added, instead of asking for a restart.", False),
    (follows_a_write_instead_of_a_restart, "Restart instead of calling this after project_config_set or project_config_adopt_hardware.", False),
    (reports_what_changed, "description_changes lists what moved.", True),
    (reports_what_changed, "description_changes is always empty.", False),
    (reports_what_changed, "description_changes never lists what moved.", False),
    (clears_the_standing_quarantine, "Clear this bench's standing quarantine instead of deleting state files.", True),
    (clears_the_standing_quarantine, "Clear this bench's quarantine instead of deleting state files.", False),
    (clears_the_standing_quarantine, "It does not clear a standing quarantine.", False),
    (touches_no_hardware, "Clear it; touches no hardware, needs permissions.allow_recover.", True),
    (touches_no_hardware, "A reason naming no hardware contact clears with no argument.", False),
    (touches_no_hardware, "It resets the board to clear the incident.", False),
    (needs_the_grant("allow_recover"), "Touches no hardware, needs permissions.allow_recover.", True),
    (needs_the_grant("allow_recover"), "Touches no hardware, needs no allow_recover.", False),
    (needs_the_grant("allow_recover"), "allow_recover is not needed.", False),
    (called_when_the_lease_status_shows_an_incident, "Call it when hardware_lease_status shows incident_stands, rather than deleting state files.", True),
    (called_when_the_lease_status_shows_an_incident, "Call it when hardware_lease_status shows incident_stands false.", False),
    (called_when_the_lease_status_shows_an_incident, "Never call it when hardware_lease_status shows incident_stands.", False),
    (called_when_the_lease_status_shows_an_incident, "Call it whatever hardware_lease_status shows.", False),
    (
        no_contact_clears_alone_and_physical_needs_a_statement,
        "A reason naming no hardware contact clears with no argument; any other answers recovery_requires_physical_check: ask the operator in chat and pass their answer verbatim as operator_statement.",
        True,
    ),
    (
        no_contact_clears_alone_and_physical_needs_a_statement,
        "A no-contact reason clears with no argument; any other answers recovery_requires_physical_check: ask the operator, pass their answer verbatim as operator_statement.",
        True,
    ),
    (
        no_contact_clears_alone_and_physical_needs_a_statement,
        "A reason naming no hardware contact answers recovery_requires_physical_check: ask the operator and pass their answer verbatim as operator_statement; any other clears with no argument.",
        False,
    ),
    (
        no_contact_clears_alone_and_physical_needs_a_statement,
        "A reason naming no hardware contact needs no argument; any other needs operator_statement: ask the operator in chat and pass their answer verbatim.",
        False,
    ),
    (audit_broken_clears_only_by_the_operator_command, "No statement clears an audit_broken reason: only its operator_command does.", True),
    (audit_broken_clears_only_by_the_operator_command, "A statement clears an audit_broken reason, as its operator_command does.", False),
    (audit_broken_clears_only_by_the_operator_command, "No statement clears an audit_broken reason.", False),
    (operator_statement_is_the_operators_words, "The operator's answer, asked in chat, passed verbatim; recorded in the ledger as theirs. Never invent one.", True),
    (operator_statement_is_the_operators_words, "Your own assessment of the board; recorded in the ledger.", False),
    (operator_statement_is_the_operators_words, "The operator's answer, asked in chat, passed verbatim; recorded in the ledger. Write one if they are away.", False),
    (accepts_a_reviewed_config_change, "Default false. True only after the operator has reviewed both digests a config_changed refusal reports; recorded in the ledger.", True),
    (accepts_a_reviewed_config_change, "Default true. True only after the operator has reviewed both digests a config_changed refusal reports; recorded in the ledger.", False),
    (accepts_a_reviewed_config_change, "Default false. Accepts any config_changed refusal; recorded in the ledger.", False),
    (upgrades_with_the_manager_that_installed_it, "Upgrade this installation to the newest release with the uv, pipx or pip that installed it.", True),
    (upgrades_with_the_manager_that_installed_it, "Upgrade to the newest release with the uv, pipx or pip that installed this copy.", True),
    (upgrades_with_the_manager_that_installed_it, "Upgrade this installation to the newest release with whichever of uv, pipx or pip is first on PATH.", False),
    (upgrades_with_the_manager_that_installed_it, "Upgrade to the version you name with the uv, pipx or pip that installed it.", False),
    (upgrades_with_the_manager_that_installed_it, "Upgrade to the newest release, not with the uv, pipx or pip that installed it.", False),
    (takes_no_arguments, "Takes no arguments, never a version you name.", True),
    (takes_no_arguments, "no arguments, needs permissions.allow_upgrade.", True),
    (takes_no_arguments, "Use it when the operator asks; it takes no arguments and needs allow_upgrade.", True),
    (takes_no_arguments, "Takes a version you name.", False),
    (takes_no_arguments, "Takes no arguments except a version.", False),
    (needs_the_grant("allow_upgrade"), "Needs permissions.allow_upgrade.", True),
    (needs_the_grant("allow_upgrade"), "Needs no allow_upgrade.", False),
    (windows_is_the_operators_command, "Windows answers upgrade_cli_only_on_host: the operator runs agentic-hil upgrade.", True),
    (windows_is_the_operators_command, "On Windows, ask the operator to run agentic-hil upgrade (upgrade_cli_only_on_host).", True),
    (windows_is_the_operators_command, "On Windows you run agentic-hil upgrade yourself (upgrade_cli_only_on_host).", False),
    (windows_is_the_operators_command, "Windows upgrades in place.", False),
    (keeps_running_the_old_release_until_restart, "running_version stays old until restart.", True),
    (keeps_running_the_old_release_until_restart, "running_version remains unchanged until this server restarts.", True),
    (keeps_running_the_old_release_until_restart, "running_version moves to the new release at once.", False),
    (keeps_running_the_old_release_until_restart, "running_version stays old until a restart, which is not needed.", False),
    (keeps_running_the_old_release_until_restart, "running_version becomes new rather than old until restart.", False),
    (failure_hands_reinstall_command_to_the_operator, "On failure the operator runs any reinstall_command.", True),
    (failure_hands_reinstall_command_to_the_operator, "On failure never run reinstall_command.", False),
    (failure_hands_reinstall_command_to_the_operator, "On failure you run reinstall_command yourself.", False),
    (
        arises_while_a_run_or_session_is_held,
        "A configuration write was attempted while this server holds hardware: a declared run, an open COM or CAN session, or a debug session.",
        True,
    ),
    (arises_while_a_run_or_session_is_held, "Refused while a session or a run is held.", True),
    (arises_while_a_run_or_session_is_held, "A configuration write was attempted while this server holds no run or session.", False),
    (arises_while_a_run_or_session_is_held, "Refused only while this server holds both a run and a session.", False),
    (arises_while_a_run_or_session_is_held, "Refused while this server holds a declared run and an open COM or CAN session.", False),
    (arises_while_a_run_or_session_is_held, "Refused while this server holds a declared run.", False),
    (arises_while_a_run_or_session_is_held, "Refused while this server holds a run or session, except a debug session.", False),
    (held_by_an_incident, "A hardware resource is held by an unresolved incident, so nothing was touched.", True),
    (held_by_an_incident, "Refused while an incident stands.", True),
    (held_by_an_incident, "A hardware resource is held by another owner, so nothing was touched.", False),
    (held_by_an_incident, "A hardware resource is not held by any incident.", False),
    (held_by_an_incident, "No incident holds the resource.", False),
    (the_quarantine_still_stands, "The ledger line is written before any marker is released, so nothing was cleared: the quarantine stands under the same quarantine_id.", True),
    (the_quarantine_still_stands, "The recovery is in the ledger, but not every marker it releases could be written, so the quarantine stands.", True),
    (the_quarantine_still_stands, "The recovery could not be written, so the quarantine no longer stands.", False),
    (the_quarantine_still_stands, "The recovery could not be written, so the quarantine was cleared.", False),
    (the_quarantine_still_stands, "The next hardware call stands the incident down.", False),
    (
        a_rerun_resumes,
        "Fix the cause, then run the recovery again; the retry is safe. A project left recovery_pending resumes from there, and the rerun's ledger line says resumed.",
        True,
    ),
    (a_rerun_resumes, "Fix the cause, then run the recovery again. A project left recovery_pending starts over, and the rerun's ledger line says fresh.", False),
    (a_rerun_resumes, "A project left recovery_pending never resumes, whatever the rerun does.", False),
    (a_rerun_resumes, "A project left `recovery_pending` starts over, and the rerun's ledger line says `resumed`.", False),
]


@pytest.mark.parametrize(("check", "text", "expected"), CONTROLS, ids=[f"{check.__name__}-{index}" for index, (check, _, _) in enumerate(CONTROLS)])
def test_each_check_accepts_the_claim_and_rejects_its_inversion(check: Callable[[str], bool], text: str, expected: bool) -> None:
    assert bool(check(text)) is expected, text


# A set definition inside the budget that keeps every keyword and inverts two
# claims an agent acts on: which grant a write needs, and that permissions only
# ever narrow.
MISLEADING_SET = (
    "Write values you choose to named keys of the configuration file. Call it after project_config_describe lists the keys, "
    "rather than editing the configuration file. Device keys need allow_config_permissions_write, permission keys "
    "allow_config_description_write, else permission_denied. Permissions also widen: closing allow_config_permissions_write "
    "freezes them all at once (permissions_frozen)."
)


def test_a_set_definition_that_inverts_the_grants_and_the_narrowing_fails_its_checks() -> None:
    assert len(MISLEADING_SET) <= DESCRIPTION_LIMIT
    assert unmet(MISLEADING_SET, binds_each_grant_to_its_half, permission_keys_only_narrow) == [
        "binds_each_grant_to_its_half",
        "permission_keys_only_narrow",
    ], MISLEADING_SET


# ---------------------------------------------------------------------------
# Shape and budget.


def test_the_six_tools_are_listed_with_descriptions_within_the_budget(listed: dict[str, dict]) -> None:
    over = {name: len(description(listed[name])) for name in TD6_TOOLS if not description(listed[name]) or len(description(listed[name])) > DESCRIPTION_LIMIT}
    assert not over, over
    properties_over = {
        (name, path): len(str(schema.get("description") or ""))
        for name in TD6_TOOLS
        for path, schema in input_properties(listed[name]["inputSchema"])
        if len(str(schema.get("description") or "")) > PROPERTY_DESCRIPTION_LIMIT
    }
    assert not properties_over, properties_over


@pytest.mark.parametrize("name", TD6_TOOLS)
def test_every_input_property_is_the_expected_one_and_describes_itself(listed: dict[str, dict], name: str) -> None:
    found = dict(input_properties(listed[name]["inputSchema"]))
    assert set(found) == PROPERTIES[name], sorted(found)
    undescribed = sorted(path for path, schema in found.items() if not str(schema.get("description") or "").strip())
    assert not undescribed, undescribed


@pytest.mark.parametrize("name", [DESCRIBE, RELOAD, UPGRADE])
def test_the_tools_that_take_no_arguments_refuse_every_argument(listed: dict[str, dict], name: str) -> None:
    schema = listed[name]["inputSchema"]
    assert schema.get("properties", {}) == {}, schema
    assert schema.get("additionalProperties") is False, schema


def test_every_boolean_states_its_default_false(listed: dict[str, dict]) -> None:
    booleans = [(name, path, schema) for name in TD6_TOOLS for path, schema in input_properties(listed[name]["inputSchema"]) if schema.get("type") == "boolean"]
    assert {(name, path) for name, path, _ in booleans} == {(ADOPT, "/apply"), (RECOVER, "/accept_config_change")}
    for name, path, schema in booleans:
        assert schema.get("default") is False, (name, path)
        assert states_default_false(str(schema.get("description") or "")), (name, path, schema.get("description"))


def test_every_string_input_refuses_the_empty_string(listed: dict[str, dict]) -> None:
    """A key, a serial, an entry id and a statement are names; an empty one names nothing."""
    strings = {(name, path): schema for name in TD6_TOOLS for path, schema in input_properties(listed[name]["inputSchema"]) if schema.get("type") == "string"}
    assert set(strings) == {(SET, "/changes/items/key"), (ADOPT, "/probe_id"), (ADOPT, "/debugger_id"), (ADOPT, "/com_port_id"), (RECOVER, "/operator_statement")}
    short = {where: schema.get("minLength") for where, schema in strings.items() if (schema.get("minLength") or 0) < 1}
    assert not short, short


def schema_property_names(node: object) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            found |= set(properties)
        for child in node.values():
            found |= schema_property_names(child)
    elif isinstance(node, list):
        for child in node:
            found |= schema_property_names(child)
    return found


def test_every_identifier_the_definitions_name_is_real(listed: dict[str, dict]) -> None:
    """A snake_case word in any of the six definitions is a listed tool, an input
    property of a listed tool, a configuration key at any depth, or a string the
    server answers with. Quoted literals are example data and are not read."""
    inputs = {path.rsplit("/", 1)[-1] for tool in listed.values() for path, _ in input_properties(tool["inputSchema"])}
    vocabulary = set(listed) | inputs | schema_property_names(config_schema()) | answer_vocabulary()
    for real in ("nothing_to_recover", "upgrade_cli_only_on_host", "writes_blocked_by_open_run", "restart_required_for", "unlocked_by", "config_reload_in_open_run"):
        assert real in vocabulary, real
    assert "config_write_in_open_session" not in vocabulary, "a near miss is still refused"

    unknown = {name: sorted(set(SNAKE_CASE.findall(re.sub(QUOTED, " ", definition_text(listed[name])))) - vocabulary) for name in TD6_TOOLS}
    assert not any(unknown.values()), unknown


def test_the_annotations_agree_with_what_the_definitions_describe(listed: dict[str, dict]) -> None:
    """Describe reads; set replaces a value; adoption writes the same values
    again; recover clears what is standing, once; upgrade reaches the package
    index and lands on the same release twice. The reload hint is a documented
    design question and is not asserted here. What a repeated call answers is
    shown by the calls themselves, further down."""
    expected = {
        DESCRIBE: {"readOnlyHint": True, "openWorldHint": False},
        SET: {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
        ADOPT: {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        RELOAD: {"openWorldHint": False},
        RECOVER: {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        UPGRADE: {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    }
    for name, hints in expected.items():
        annotations = listed[name].get("annotations", {})
        assert {key: annotations.get(key) for key in hints} == hints, (name, annotations)


# ---------------------------------------------------------------------------
# What each definition has to say.


def test_project_config_describe_says_what_it_lists_that_it_is_free_and_when_writes_are_blocked(listed: dict[str, dict]) -> None:
    text = description(listed[DESCRIBE])
    assert unmet(text, lists_writable_and_locked_keys, reads_before_setting, needs_no_permission, flags_a_held_bench, tells_bad_yaml_from_unreadable) == [], text


def test_project_config_set_says_which_grant_opens_what_and_when_a_write_is_in_force(listed: dict[str, dict]) -> None:
    tool = listed[SET]
    text = description(tool)
    assert unmet(
        text,
        key_is_listed_by_describe,
        binds_each_grant_to_its_half,
        permission_keys_only_narrow,
        names_the_refusal_without_a_grant,
        closing_a_config_grant_binds_at_once,
        freezes_permissions_when_the_grant_closes,
    ) == [], text
    assert unmet(property_text(tool, "/changes"), changes_are_all_or_nothing) == [], property_text(tool, "/changes")
    key = property_text(tool, "/changes/items/key")
    assert unmet(key, key_is_listed_by_describe, not_in_force_until_reread, reload_scope_is_named) == [], key
    value = property_text(tool, "/changes/items/value")
    assert unmet(value, value_is_one_scalar_matching_its_schema, permission_keys_take_only_false) == [], value


def test_project_config_adopt_hardware_says_what_it_reads_fills_keeps_and_refuses(listed: dict[str, dict]) -> None:
    tool = listed[ADOPT]
    text = description(tool)
    assert unmet(
        text,
        fills_only_placeholders_from_what_is_attached,
        no_flash_or_erase_and_no_reset_claim,
        writes_nothing_without_apply,
        unknown_state_may_reset_into_halt,
    ) == [], text
    assert unmet(definition_text(tool), tells_to_reload_after_writing, no_flash_or_erase_and_no_reset_claim) == [], definition_text(tool)
    assert unmet(property_text(tool, "/apply"), apply_writes_through_project_config_set, nothing_carried_answers_applied_false) == [], property_text(tool, "/apply")
    assert unmet(property_text(tool, "/probe_id"), probe_id_defaults_and_disambiguates, bounded_per_step(10)) == [], property_text(tool, "/probe_id")
    assert unmet(property_text(tool, "/debugger_id"), debugger_id_names_a_configured_entry, refuses_another_board) == [], property_text(tool, "/debugger_id")
    com_port = property_text(tool, "/com_port_id")
    assert unmet(com_port, com_port_default_chain, created_with_every_permission_false, several_unnamed_ports_answer_unavailable) == [], com_port


def test_project_config_reload_description_says_what_it_takes_what_it_never_takes_and_when_it_is_refused(listed: dict[str, dict]) -> None:
    text = description(listed[RELOAD])
    assert unmet(
        text,
        rereads_the_four_sections,
        follows_a_write_instead_of_a_restart,
        reports_what_changed,
        never_takes_permissions_or_debug,
        new_devices_arrive_granted_nothing,
        changed_permissions_need_a_restart,
    ) == [], text


def test_hardware_recover_says_what_it_clears_what_it_needs_and_whose_words_a_statement_is(listed: dict[str, dict]) -> None:
    tool = listed[RECOVER]
    text = description(tool)
    assert unmet(
        text,
        clears_the_standing_quarantine,
        called_when_the_lease_status_shows_an_incident,
        touches_no_hardware,
        needs_the_grant("allow_recover"),
        no_contact_clears_alone_and_physical_needs_a_statement,
    ) == [], text
    assert unmet(definition_text(tool), audit_broken_clears_only_by_the_operator_command) == [], definition_text(tool)
    assert unmet(property_text(tool, "/operator_statement"), operator_statement_is_the_operators_words) == [], property_text(tool, "/operator_statement")
    assert unmet(property_text(tool, "/accept_config_change"), accepts_a_reviewed_config_change) == [], property_text(tool, "/accept_config_change")


def test_server_upgrade_says_what_it_runs_what_it_needs_and_what_this_server_keeps_running(listed: dict[str, dict]) -> None:
    text = description(listed[UPGRADE])
    assert unmet(
        text,
        upgrades_with_the_manager_that_installed_it,
        takes_no_arguments,
        needs_the_grant("allow_upgrade"),
        windows_is_the_operators_command,
        bounded_per_step(600),
        keeps_running_the_old_release_until_restart,
        failure_hands_reinstall_command_to_the_operator,
    ) == [], text


# The refusals the definitions name no longer, and the entry an agent reads for
# each: set and adoption refused under a hold (config_write_in_open_run), the
# reload under a hold or an incident, the upgrade under a hold, and a recovery
# that could not be written down or could not release every marker.
CATALOGUE_CLAIMS: list[tuple[str, str, Callable[[str], bool]]] = [
    ("config_write_in_open_run", "meaning", arises_while_a_run_or_session_is_held),
    ("config_reload_in_open_run", "meaning", arises_while_a_run_or_session_is_held),
    ("upgrade_in_open_run", "meaning", arises_while_a_run_or_session_is_held),
    ("resource_quarantined", "meaning", held_by_an_incident),
    ("recovery_audit_failed", "meaning", the_quarantine_still_stands),
    ("recovery_persist_failed", "meaning", the_quarantine_still_stands),
    ("recovery_persist_failed", "remediation", a_rerun_resumes),
]


@pytest.mark.parametrize(("error_type", "field", "check"), CATALOGUE_CLAIMS, ids=[f"{error_type}-{field}" for error_type, field, _ in CATALOGUE_CLAIMS])
def test_the_catalogue_says_what_the_definitions_leave_to_it(tmp_path: Path, error_type: str, field: str, check: Callable[[str], bool]) -> None:
    # The entry `resources/read` serves for `error_type`, the one a refusal's
    # catalogue link resolves to.
    service = new_service(tmp_path / "catalogue")
    try:
        value = json.loads(read_text(service, ERROR_URI_PREFIX + error_type))[field]
    finally:
        close(service)
    text = " ".join(value) if isinstance(value, list) else str(value)
    assert check(text), (error_type, field, text)


# ---------------------------------------------------------------------------
# The claims, as the code answers them today. Holds first: this server's run
# or COM session refuses every write, the reload and the upgrade.


@pytest.mark.parametrize("hold", ["run", "com_session"])
def test_a_run_or_session_this_server_holds_refuses_writes_reload_and_upgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hold: str) -> None:
    workspace, path = reload_bench(tmp_path, monkeypatch, com_ports_yaml=HOLD_PORTS, probe_id=HOLD_PROBE)
    rewrite(path, lambda document: document.__setitem__("permissions", {CONFIG_DESCRIPTION_RIGHT: True, CONFIG_PERMISSIONS_RIGHT: True, "allow_upgrade": True}))
    line = install_line(monkeypatch)
    monkeypatch.setattr("agentic_hil.adopt.discover_attached_hardware", lambda *args, **kwargs: pytest.fail("a held bench reached hardware discovery"))
    monkeypatch.setattr("agentic_hil.upgrade._host_locks_running_files", lambda: False)
    never_runs(monkeypatch)
    service = open_service(workspace)
    try:
        assert call(service, DESCRIBE, {})["writes_blocked_by_open_run"] is False
        if hold == "run":
            started = call(service, "bench_run_start", {"devices": [{"kind": "debugger", "id": "dut"}]})
        else:
            started = call(service, "com_session_start", {"port_id": "dut_uart"})
        assert started["ok"] is True, started
        before = path.read_bytes()

        described = call(service, DESCRIBE, {})
        assert described["ok"] is True, described
        assert described["writes_blocked_by_open_run"] is True, described
        written = call(service, SET, changes(("com_ports.dut_uart.baudrate", 9600)))
        assert written["error_type"] == "config_write_in_open_run", written
        adopted = call(service, ADOPT, {})
        assert adopted["error_type"] == "config_write_in_open_run", adopted
        reloaded = call(service, RELOAD, {})
        assert reloaded["error_type"] == RELOAD_IN_OPEN_RUN_ERROR, reloaded
        upgraded = call(service, UPGRADE, {})
        assert upgraded["error_type"] == "upgrade_in_open_run", upgraded
        assert path.read_bytes() == before
    finally:
        for handle in line.handles:
            handle.unstall.set()
        close(service)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        pytest.param(SET, {"changes": [{"key": "", "value": 9600}]}, id="set-key"),
        pytest.param(ADOPT, {"probe_id": ""}, id="adopt-probe_id"),
        pytest.param(ADOPT, {"debugger_id": ""}, id="adopt-debugger_id"),
        pytest.param(ADOPT, {"com_port_id": ""}, id="adopt-com_port_id"),
        pytest.param(RECOVER, {"operator_statement": ""}, id="recover-operator_statement"),
    ],
)
def test_an_empty_name_is_refused_before_anything_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, arguments: dict) -> None:
    workspace, path = write_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    monkeypatch.setattr("agentic_hil.adopt.discover_attached_hardware", lambda *args, **kwargs: pytest.fail("an empty name reached hardware discovery"))
    before = path.read_bytes()
    service = open_service(workspace)
    try:
        refused = call(service, name, arguments)
    finally:
        close(service)

    assert refused["error_type"] == "invalid_argument", refused
    assert path.read_bytes() == before


# project_config_describe


def test_describe_lists_open_keys_with_their_values_and_locked_keys_with_the_grant_that_opens_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = write_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    service = open_service(workspace)
    try:
        described = call(service, DESCRIBE, {})
    finally:
        close(service)

    assert described["ok"] is True, described
    assert described["writable_keys"] and described["locked_keys"], described
    for entry in described["writable_keys"]:
        assert {"key", "current_value", "value_schema"} <= set(entry), entry
        assert "unlocked_by" not in entry, entry
    assert {entry["unlocked_by"] for entry in described["locked_keys"]} == {f"permissions.{CONFIG_PERMISSIONS_RIGHT}"}
    assert described["writes_blocked_by_open_run"] is False


def test_describe_needs_no_permission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = write_bench(tmp_path, monkeypatch)
    service = open_service(workspace)
    try:
        described = call(service, DESCRIBE, {})
    finally:
        close(service)

    assert described["ok"] is True, described
    assert described["writable_keys"] == []
    assert {entry["unlocked_by"] for entry in described["locked_keys"]} == {f"permissions.{CONFIG_DESCRIPTION_RIGHT}", f"permissions.{CONFIG_PERMISSIONS_RIGHT}"}


def unreadable(path: Path) -> None:
    """The same configuration, saved as UTF-16 the way a Windows editor may save it."""
    path.write_bytes(path.read_text(encoding="utf-8").encode("utf-16"))


def does_not_parse(path: Path) -> None:
    path.write_text("target: [this does not close\n", encoding="utf-8")


FILE_FAULTS = [
    pytest.param(does_not_parse, "config_invalid", id="bad-yaml"),
    pytest.param(unreadable, "config_unreadable", id="not-utf8"),
]


@pytest.mark.parametrize(("spoil", "error_type"), FILE_FAULTS)
def test_describe_tells_a_file_that_does_not_parse_from_one_that_cannot_be_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spoil: Callable[[Path], None], error_type: str) -> None:
    workspace, path = write_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    service = open_service(workspace)
    try:
        spoil(path)
        described = call(service, DESCRIBE, {})
    finally:
        close(service)

    assert described["ok"] is False, described
    assert described["error_type"] == error_type, described
    assert described["writable_keys"] == [] and described["locked_keys"] == []


def test_describe_after_the_file_is_gone_answers_out_of_the_policy_this_server_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = write_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    service = open_service(workspace)
    try:
        path.unlink()
        described = call(service, DESCRIBE, {})
    finally:
        close(service)

    assert described["ok"] is True, described
    assert described["document_source"] == "loaded_policy", described


@pytest.mark.parametrize("name", [DESCRIBE, SET, ADOPT, RELOAD])
def test_a_workspace_with_no_configuration_sends_every_configuration_tool_to_project_config_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr("agentic_hil.adopt.discover_attached_hardware", lambda *args, **kwargs: pytest.fail("an unprovisioned workspace reached hardware discovery"))
    unprovisioned = UnprovisionedToolService(unprovisioned_bench(tmp_path, monkeypatch))
    try:
        refused = unprovisioned.call(name, changes(("target.name", "td6")) if name == SET else {})
    finally:
        unprovisioned.close()

    assert refused["error_type"] == "config_file_not_found", refused
    assert PROJECT_CONFIG_CREATE in refused["next_step"], refused


# project_config_set

HALVES = {
    CONFIG_DESCRIPTION_RIGHT: ("com_ports.dut_uart.baudrate", 9600),
    CONFIG_PERMISSIONS_RIGHT: ("debuggers.dut.permissions.allow_flash", False),
}


@pytest.mark.parametrize("grant", sorted(HALVES))
def test_each_grant_opens_its_own_half_and_the_other_half_answers_permission_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grant: str) -> None:
    (other,) = set(HALVES) - {grant}
    workspace, path = write_bench(tmp_path, monkeypatch, device_permissions={"allow_flash": True}, **{grant: True})
    service = open_service(workspace)
    try:
        opened = call(service, SET, changes(HALVES[grant]))
        before = path.read_bytes()
        refused = call(service, SET, changes(HALVES[other]))
    finally:
        close(service)

    assert opened["ok"] is True, opened
    assert refused["error_type"] == "permission_denied", refused
    assert refused["permission"] == f"permissions.{other}", refused
    assert path.read_bytes() == before


def test_a_permission_key_takes_only_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = write_bench(tmp_path, monkeypatch, device_permissions={"allow_flash": True}, **{CONFIG_PERMISSIONS_RIGHT: True})
    before = path.read_bytes()
    service = open_service(workspace)
    try:
        widened = call(service, SET, changes(("debuggers.dut.permissions.allow_reset", True)))
    finally:
        close(service)

    assert widened["error_type"] == "permission_widening_denied", widened
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(changes(("target.name", "td6-a"), ("target.name", "td6-b")), id="repeated-key"),
        pytest.param(changes(("target.td6_unknown", "x")), id="unknown-key"),
        pytest.param(changes(("com_ports.dut_uart.baudrate", "as fast as it goes")), id="value-the-schema-refuses"),
        pytest.param(changes(("target.name", "td6-renamed"), ("com_ports.dut_uart.baudrate", "as fast as it goes")), id="one-valid-one-refused"),
        pytest.param({"changes": []}, id="no-change"),
        pytest.param({"changes": [{"key": "target.name", "value": {"name": "td6"}}]}, id="object-value"),
        pytest.param({"changes": [{"key": "target.name", "value": ["td6"]}]}, id="array-value"),
    ],
)
def test_a_refused_change_writes_nothing_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    workspace, path = write_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    before = path.read_bytes()
    service = open_service(workspace)
    try:
        refused = call(service, SET, payload)
    finally:
        close(service)

    assert refused["error_type"] == "invalid_argument", refused
    assert path.read_bytes() == before


def test_a_written_key_is_not_in_force_until_the_description_is_reread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = write_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    service = open_service(workspace)
    try:
        written = call(service, SET, changes(("com_ports.dut_uart.baudrate", 9600)))
        assert written["ok"] is True, written
        assert written["reload_required"] is True, written
        assert document_of(path)["com_ports"]["dut_uart"]["baudrate"] == 9600
        assert service.config.com_ports["dut_uart"].baudrate == 115200, "the loaded configuration is kept"

        reloaded = call(service, RELOAD, {})
        assert reloaded["ok"] is True, reloaded
        assert "com_ports.dut_uart.baudrate" in reloaded["description_changes"], reloaded
        assert service.config.com_ports["dut_uart"].baudrate == 9600
    finally:
        close(service)


def test_a_reread_takes_the_device_sections_and_leaves_debug_and_permissions_for_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One write across three halves of the file, one re-read: only the device
    description moves; the device grant and the debug section stay as loaded."""
    workspace, path = write_bench(tmp_path, monkeypatch, device_permissions={"allow_flash": True}, **{CONFIG_DESCRIPTION_RIGHT: True, CONFIG_PERMISSIONS_RIGHT: True})
    service = open_service(workspace)
    try:
        gdb_in_force = service.config.debug.gdb_executable
        written = call(
            service,
            SET,
            changes(("com_ports.dut_uart.baudrate", 9600), ("debuggers.dut.permissions.allow_flash", False), ("debug.gdb_executable", sys.executable)),
        )
        assert written["ok"] is True, written
        assert written["reload_required"] is True, written
        assert document_of(path)["debug"]["gdb_executable"] == sys.executable

        reloaded = call(service, RELOAD, {})
        assert reloaded["ok"] is True, reloaded
        assert reloaded["description_changes"] == ["com_ports.dut_uart.baudrate"], reloaded
        assert reloaded["restart_required_for"] == ["debuggers.dut.allow_flash"], reloaded
        assert {"debug", "permissions", "<entry>.permissions"} <= {entry["section"] for entry in reloaded["not_reloaded_sections"]}, reloaded
        assert service.config.com_ports["dut_uart"].baudrate == 9600
        assert service.config.debuggers["dut"].permissions.allow_flash is True, "the grant this server loaded stays in force"
        assert service.config.debug.gdb_executable == gdb_in_force, "the debug section is not re-read"
    finally:
        close(service)


def test_closing_the_description_grant_binds_the_next_write_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = write_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True, CONFIG_PERMISSIONS_RIGHT: True})
    service = open_service(workspace)
    try:
        closed = call(service, SET, changes((f"permissions.{CONFIG_DESCRIPTION_RIGHT}", False)))
        assert closed["ok"] is True, closed
        before = path.read_bytes()
        refused = call(service, SET, changes(("com_ports.dut_uart.baudrate", 9600)))
    finally:
        close(service)

    assert refused["error_type"] == "permission_denied", refused
    assert refused["permission"] == f"permissions.{CONFIG_DESCRIPTION_RIGHT}", refused
    assert path.read_bytes() == before


def test_closing_the_permissions_grant_freezes_every_permission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = write_bench(tmp_path, monkeypatch, device_permissions={"allow_flash": True}, **{CONFIG_PERMISSIONS_RIGHT: True})
    service = open_service(workspace)
    try:
        closed = call(service, SET, changes((f"permissions.{CONFIG_PERMISSIONS_RIGHT}", False)))
        assert closed["ok"] is True, closed
        before = path.read_bytes()
        refused = call(service, SET, changes(("debuggers.dut.permissions.allow_flash", False)))
    finally:
        close(service)

    assert closed["permissions_frozen"]["closed_key"] == f"permissions.{CONFIG_PERMISSIONS_RIGHT}", closed
    assert closed["permissions_frozen"]["frozen_permissions"]["debuggers.dut.permissions.allow_flash"] is True, closed
    assert refused["error_type"] == "permission_denied", refused
    assert refused["permission"] == f"permissions.{CONFIG_PERMISSIONS_RIGHT}", refused
    assert path.read_bytes() == before


# project_config_adopt_hardware


def td6_attached(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> dict:
    """One attached probe, its target and its COM port, under this module's names."""
    found: dict[str, Any] = {
        "probe_id": ADOPT_PROBE,
        "target": {"probe_id": ADOPT_PROBE, "controller": "STM32F446RE"},
        "com_port": {"device": ADOPT_PORT, "serial_number": ADOPT_PROBE},
        "available_com_ports": {"ok": True, "ports": [{"device": ADOPT_PORT, "serial_number": ADOPT_PROBE}]},
    }
    return attached(monkeypatch, **{**found, **overrides})


def with_document(path: Path, edit: Callable[[dict], None]) -> None:
    document = document_of(path)
    edit(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def keys(entries: list[dict]) -> set[str]:
    return {str(entry["key"]) for entry in entries}


def test_adoption_plans_without_apply_and_writes_with_it_not_in_force_until_reloaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    td6_attached(monkeypatch)
    before = path.read_bytes()
    service = open_service(workspace)
    try:
        planned = call(service, ADOPT, {})
        assert planned["ok"] is True, planned
        assert "debuggers.dut.probe_id" in keys(planned["carried"]), planned
        assert path.read_bytes() == before, "the plan writes nothing"

        applied = call(service, ADOPT, {"apply": True})
        assert applied["ok"] is True, applied
        assert applied["reload_required"] is True, applied
        assert applied["write"]["tool"] == "project_config_set", applied["write"]
        assert document_of(path)["debuggers"]["dut"]["probe_id"] == ADOPT_PROBE
        assert service.config.debuggers["dut"].probe_id is None, "not in force before the description is re-read"

        reloaded = call(service, RELOAD, {})
        assert reloaded["ok"] is True, reloaded
        assert service.config.debuggers["dut"].probe_id == ADOPT_PROBE
    finally:
        close(service)


def test_a_second_apply_over_the_adopted_values_writes_nothing_and_answers_applied_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    td6_attached(monkeypatch)
    service = open_service(workspace)
    try:
        first = call(service, ADOPT, {"apply": True})
        assert first["ok"] is True and first["applied"] is True, first
        after_first = path.read_bytes()
        second = call(service, ADOPT, {"apply": True})
    finally:
        close(service)

    assert second["ok"] is True, second
    assert second["carried"] == [], second
    assert second["applied"] is False, second
    assert path.read_bytes() == after_first


def test_adoption_without_the_description_grant_answers_permission_denied_and_keeps_the_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = placeholder_bench(tmp_path, monkeypatch)
    td6_attached(monkeypatch)
    before = path.read_bytes()
    service = open_service(workspace)
    try:
        refused = call(service, ADOPT, {"apply": True})
    finally:
        close(service)

    assert refused["error_type"] == "permission_denied", refused
    assert refused["permission"] == f"permissions.{CONFIG_DESCRIPTION_RIGHT}", refused
    assert refused["carried"], refused
    assert refused["write"]["tool"] == "project_config_set", refused["write"]
    assert path.read_bytes() == before


def test_adoption_keeps_what_somebody_set_and_names_what_was_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})

    def configured(document: dict) -> None:
        document["debuggers"]["dut"]["probe_id"] = ADOPT_PROBE
        document["target"]["controller"] = "stm32f411re"

    with_document(path, configured)
    td6_attached(monkeypatch, com_port=None, available_com_ports={"ok": True, "ports": []})
    service = open_service(workspace)
    try:
        planned = call(service, ADOPT, {})
    finally:
        close(service)

    assert planned["ok"] is True, planned
    kept = {entry["key"]: entry for entry in planned["kept"]}
    assert kept["target.controller"]["configured_value"] == "stm32f411re", kept
    assert kept["target.controller"]["discovered_value"] == "stm32f446re", kept
    assert "target.controller" not in keys(planned["carried"])
    assert "com_ports.<name>.device" in keys(planned["unavailable"]), planned["unavailable"]


def test_an_entry_naming_another_probe_answers_hardware_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    with_document(path, lambda document: document["debuggers"]["dut"].update({"probe_id": OTHER_PROBE}))
    before = path.read_bytes()
    td6_attached(monkeypatch)
    service = open_service(workspace)
    try:
        refused = call(service, ADOPT, {"apply": True})
    finally:
        close(service)

    assert refused["error_type"] == "hardware_mismatch", refused
    assert refused["configured_probe_id"] == OTHER_PROBE
    assert refused["discovered_probe_id"] == ADOPT_PROBE
    assert path.read_bytes() == before


def test_debugger_id_names_a_configured_entry_and_never_adds_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    td6_attached(monkeypatch)
    service = open_service(workspace)
    try:
        unknown = call(service, ADOPT, {"apply": True, "debugger_id": "td6_second_board"})
    finally:
        close(service)
    assert unknown["error_type"] == "unknown_device", unknown
    assert "td6_second_board" not in document_of(path)["debuggers"]

    def spare(document: dict) -> None:
        document["debuggers"]["spare"] = {**document["debuggers"]["dut"], "probe_id": None, "resource_id": "td6-spare-board"}

    with_document(path, spare)
    service = open_service(workspace)
    try:
        several = call(service, ADOPT, {"apply": True})
    finally:
        close(service)
    assert several["error_type"] == "invalid_argument", several
    assert several["field"] == "debugger_id", several


def test_probe_id_defaults_to_the_configured_one_and_selects_among_attached_probes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    with_document(path, lambda document: document["debuggers"]["dut"].update({"probe_id": CONFIGURED_PROBE}))
    discovery = td6_attached(monkeypatch)
    service = open_service(workspace)
    try:
        call(service, ADOPT, {})
        assert discovery["_selected"]["probe_id"] == CONFIGURED_PROBE
        call(service, ADOPT, {"probe_id": ADOPT_PROBE})
        assert discovery["_selected"]["probe_id"] == ADOPT_PROBE
    finally:
        close(service)


def test_several_attached_probes_and_no_serial_answer_ambiguous_hardware(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    _fixed_stlink(monkeypatch, f"ST-LINK SN : {ADOPT_PROBE}\nST-LINK SN : {OTHER_PROBE}\n")
    before = path.read_bytes()
    service = open_service(workspace)
    try:
        refused = call(service, ADOPT, {"apply": True})
    finally:
        close(service)

    assert refused["error_type"] == "ambiguous_hardware", refused
    assert sorted(entry["probe_id"] for entry in refused["probes"]) == sorted([ADOPT_PROBE, OTHER_PROBE])
    assert path.read_bytes() == before


def stlink_reads(monkeypatch: pytest.MonkeyPatch, listing: str) -> list[tuple[list[str], float]]:
    """The real discovery path with the two ST-Link processes replaced, and each
    process it asked for recorded with the bound it was given."""
    responses = iter(
        [
            CompletedCommand(listing, "", 0, False, False),
            CompletedCommand("ST-LINK SN : IGNORED\nDevice name : STM32F446RE\n", "", 0, False, False),
        ]
    )
    asked: list[tuple[list[str], float]] = []

    def spawn(command: list[str], cwd: str, timeout_s: float) -> CompletedCommand:
        asked.append((command, timeout_s))
        return next(responses)

    monkeypatch.setattr("agentic_hil.bootstrap.find_stm32_programmer_cli", lambda: str(Path("C:/ST/STM32_Programmer_CLI.exe")))
    monkeypatch.setattr("agentic_hil.bootstrap.spawn_command", spawn)
    monkeypatch.setattr("agentic_hil.bootstrap.list_available_com_ports", lambda tool: {"ok": True, "ports": []})
    return asked


def test_a_read_that_succeeds_runs_two_listing_processes_bounded_at_ten_seconds_each_and_no_acting_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _ = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    asked = stlink_reads(monkeypatch, f"ST-LINK SN : {ADOPT_PROBE}\n")
    service = open_service(workspace)
    try:
        planned = call(service, ADOPT, {})
    finally:
        close(service)

    assert planned["ok"] is True, planned
    assert [timeout for _, timeout in asked] == [10.0, 10.0], asked
    commands = [command for command, _ in asked]
    assert any("mode=HOTPLUG" in command for command in commands), commands
    acting = {"-w", "-d", "-e", "-rst", "-hardRst", "-halt", "-s", "-ob"}
    assert not [argument for command in commands for argument in command if argument in acting or argument.startswith("mode=UR")], commands


@pytest.mark.parametrize(("policy", "recovery"), [(None, ["reset_target:halt", "probe_target"]), ("off", [])], ids=["default-policy", "recovery-off"])
def test_a_read_that_leaves_the_board_state_unknown_answers_resource_quarantined_and_may_reset_it_into_halt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str | None, recovery: list[str]
) -> None:
    """The one way adoption reaches a reset: not the read, the recovery after a
    read that was reaped mid-attach, as the bench's policy allows."""
    workspace, path = placeholder_bench(tmp_path, monkeypatch, permissions=DEFAULT_TEST_PERMISSIONS, **{CONFIG_DESCRIPTION_RIGHT: True})
    if policy is not None:
        _set_auto_recover(path, policy)
    before = path.read_bytes()
    monkeypatch.setattr("agentic_hil.adopt.discover_attached_hardware", _timed_out_read(probe_serial=ADOPT_PROBE))
    backend = _RecoveryBackend()
    service = AgenticHILToolService(load_authoritative_config(workspace), backend=backend, frontend="mcp")
    try:
        refused = call(service, ADOPT, {"apply": True})
    finally:
        close(service)

    assert refused["error_type"] == "resource_quarantined", refused
    assert backend.calls == recovery, backend.calls
    assert path.read_bytes() == before


def test_a_com_port_adoption_creates_carries_every_permission_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = placeholder_bench(tmp_path, monkeypatch, **{CONFIG_DESCRIPTION_RIGHT: True})
    td6_attached(monkeypatch)
    service = open_service(workspace)
    try:
        applied = call(service, ADOPT, {"apply": True})
    finally:
        close(service)

    assert applied["ok"] is True, applied
    ports = document_of(path)["com_ports"]
    assert len(ports) == 1, ports
    (entry,) = ports.values()
    assert entry["device"] == ADOPT_PORT
    assert entry["permissions"] and not any(entry["permissions"].values()), entry


# project_config_reload_description


def test_reload_takes_a_description_change_and_needs_no_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = reload_bench(tmp_path, monkeypatch)
    service = open_service(workspace)
    try:
        assert not any(getattr(service.config.permissions, grant) for grant in (CONFIG_DESCRIPTION_RIGHT, CONFIG_PERMISSIONS_RIGHT))
        rewrite(path, lambda document: document["com_ports"]["dut_uart"].update({"baudrate": 9600}))
        reloaded = call(service, RELOAD, {})
        assert reloaded["ok"] is True, reloaded
        assert reloaded["description_changes"] == ["com_ports.dut_uart.baudrate"], reloaded
        assert service.config.com_ports["dut_uart"].baudrate == 9600

        again = call(service, RELOAD, {})
        assert again["ok"] is True, again
        assert again["description_changes"] == [], "a second re-read of the same file lists nothing"
    finally:
        close(service)


def test_reload_leaves_a_changed_grant_for_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = reload_bench(tmp_path, monkeypatch)
    service = open_service(workspace)
    try:
        assert service.config.debuggers["dut"].permissions.allow_flash is True
        rewrite(path, lambda document: document["debuggers"]["dut"]["permissions"].update({"allow_flash": False}))
        reloaded = call(service, RELOAD, {})
        assert reloaded["ok"] is True, reloaded
        assert reloaded["restart_required_for"] == ["debuggers.dut.allow_flash"], reloaded
        assert reloaded["description_changes"] == [], reloaded
        assert service.config.debuggers["dut"].permissions.allow_flash is True, "the grant this server loaded stays in force"
    finally:
        close(service)


def test_reload_gives_a_new_device_every_permission_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = reload_bench(tmp_path, monkeypatch)
    service = open_service(workspace)
    try:
        rewrite(path, add_second_board)
        reloaded = call(service, RELOAD, {})
        assert reloaded["ok"] is True, reloaded
        assert not any(asdict(service.config.debuggers["spare"].permissions).values())
        assert not any(asdict(service.config.com_ports["spare_uart"].permissions).values())
    finally:
        close(service)


# The re-read goes through the startup loader, which names a file that is not
# UTF-8 config_invalid where describe names it config_unreadable.
RELOAD_FILE_FAULTS = [
    pytest.param(does_not_parse, "config_invalid", id="bad-yaml"),
    pytest.param(unreadable, "config_invalid", id="not-utf8"),
]


@pytest.mark.parametrize(("spoil", "error_type"), RELOAD_FILE_FAULTS)
def test_reload_of_a_file_that_cannot_be_loaded_rereads_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spoil: Callable[[Path], None], error_type: str) -> None:
    workspace, path = reload_bench(tmp_path, monkeypatch)
    service = open_service(workspace)
    try:
        baudrate = service.config.com_ports["dut_uart"].baudrate
        spoil(path)
        refused = call(service, RELOAD, {})
        assert refused["error_type"] == error_type, refused
        assert service.config.com_ports["dut_uart"].baudrate == baudrate
        assert sorted(service.config.debuggers) == ["dut"]
    finally:
        close(service)


def test_reload_is_refused_while_an_incident_stands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, path = reload_bench(tmp_path, monkeypatch)
    service = open_service(workspace)
    try:
        service.coordinator.blocked = True
        service.coordinator.audit_incident = True
        service.coordinator.quarantine_id = "td6incident"
        rewrite(path, add_second_board)
        refused = call(service, RELOAD, {})
        assert refused["error_type"] == "resource_quarantined", refused
        assert sorted(service.config.debuggers) == ["dut"]
    finally:
        service.coordinator.blocked = False
        close(service)


# hardware_recover


def standing_incident(config: Any, reason: str) -> str:
    """A standing incident on this module's own resource, left by an owner that is gone."""
    owner = HardwareCoordinator(config, "td6-setup")
    lease = owner.acquire(RECOVER_RESOURCE)
    lease.quarantine(reason, audit_broken=True)
    incident = owner.quarantine_id
    owner.close()
    assert isinstance(incident, str)
    return incident


@pytest.fixture
def no_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recovery starts no process and opens no port."""
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("recovery started a process"))
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=lambda *args, **kwargs: pytest.fail("recovery opened a port")))


def recover_service(config: Any) -> AgenticHILToolService:
    return AgenticHILToolService(config, frontend="mcp")


def lease_status(service: AgenticHILToolService) -> dict:
    """hardware_lease_status through `tools/call`. A standing incident flags it
    isError, so it is read as it is answered rather than through `call`."""
    response = handle_mcp_message(tools_call(1, "hardware_lease_status", {}), service)
    assert isinstance(response, dict) and "result" in response, response
    return response["result"]["structuredContent"]


def test_recover_with_nothing_standing_answers_nothing_to_recover_without_the_grant(tmp_path: Path, no_hardware: None) -> None:
    config = config_for(tmp_path, allow_recover=False)
    service = recover_service(config)
    try:
        answered = call(service, RECOVER, {})
    finally:
        close(service)

    assert answered["ok"] is True, answered
    assert answered["nothing_to_recover"] is True, answered


def test_recover_without_the_grant_answers_permission_denied(tmp_path: Path, no_hardware: None) -> None:
    config = config_for(tmp_path, allow_recover=False)
    incident = standing_incident(config, LEASE_RELEASE_RETRY_REASON)
    service = recover_service(config)
    try:
        refused = call(service, RECOVER, {})
        assert service.coordinator.status()["incident_stands"] is True
    finally:
        close(service)

    assert refused["error_type"] == "permission_denied", refused
    assert refused["permission"] == "permissions.allow_recover", refused
    assert refused["operator_command"] == recovery_operator_command(incident)


def test_a_no_contact_reason_clears_with_no_argument_and_touches_no_hardware(tmp_path: Path, no_hardware: None) -> None:
    """hardware_lease_status shows the incident standing, the recovery clears it, and the status shows it gone."""
    config = config_for(tmp_path)
    incident = standing_incident(config, LEASE_RELEASE_RETRY_REASON)
    service = recover_service(config)
    try:
        before = lease_status(service)
        cleared = call(service, RECOVER, {})
        after = lease_status(service)
        again = call(service, RECOVER, {})
    finally:
        close(service)

    assert before["incident_stands"] is True, before
    assert after["incident_stands"] is False, after
    assert cleared["ok"] is True, cleared
    assert cleared["recovered_quarantine_id"] == incident
    assert cleared["resumed"] is False, cleared
    assert cleared["config_change_accepted"] is False, cleared
    assert again["ok"] is True and again["nothing_to_recover"] is True, again


def test_any_other_reason_asks_for_the_operators_statement_and_records_it_verbatim(tmp_path: Path, no_hardware: None) -> None:
    config = config_for(tmp_path)
    standing_incident(config, "safe_state_unconfirmed")
    said = "TD6 board is powered, idle and on the bench, I looked."
    service = recover_service(config)
    try:
        asked = call(service, RECOVER, {})
        assert asked["error_type"] == RECOVERY_PHYSICAL_CHECK_ERROR, asked
        assert asked["missing_argument"] == "operator_statement", asked
        cleared = call(service, RECOVER, {"operator_statement": said})
        assert cleared["ok"] is True, cleared
    finally:
        close(service)

    (line,) = ledger(config)
    assert line["operator_statement"] == said
    assert line["attestation"] == "operator_statement_via_agent"


def test_no_statement_clears_an_audit_broken_reason_and_its_operator_command_does(tmp_path: Path, no_hardware: None) -> None:
    config = config_for(tmp_path)
    incident = standing_incident(config, "audit_broken")
    service = recover_service(config)
    try:
        refused = call(service, RECOVER, {"operator_statement": "TD6 operator: it is fine, I looked."})
        assert refused["ok"] is False, refused
        assert refused["operator_command"] == recovery_operator_command(incident), refused
        assert service.coordinator.status()["incident_stands"] is True
    finally:
        close(service)

    operator = HardwareCoordinator(config, "td6-operator")
    try:
        recovered = operator.recover(safe_state_confirmed=True, quarantine_id=incident)
    finally:
        operator.close()
    assert recovered["ok"] is True, recovered


def test_accept_config_change_clears_a_config_changed_refusal_and_is_recorded(tmp_path: Path, no_hardware: None) -> None:
    config = config_for(tmp_path)
    standing_incident(config, LEASE_RELEASE_RETRY_REASON)
    recorded = lease_config_sha256(config)
    edited = edit_config(tmp_path)
    service = recover_service(edited)
    try:
        refused = call(service, RECOVER, {})
        assert refused["error_type"] == "config_changed", refused
        accepted = call(service, RECOVER, {"accept_config_change": True})
        assert accepted["ok"] is True, accepted
        assert accepted["config_change_accepted"] is True, accepted
    finally:
        close(service)

    (line,) = ledger(edited)
    assert line["config_change_accepted"] is True
    assert line["recorded_config_sha256"] == recorded


def test_a_recovery_another_owner_is_holding_answers_resource_busy_and_keeps_the_incident(tmp_path: Path, no_hardware: None) -> None:
    config = config_for(tmp_path)
    incident = standing_incident(config, LEASE_RELEASE_RETRY_REASON)
    other = HardwareCoordinator(config, "td6-busy")
    lock = other._acquire_lock(other.project_key, [other.project_key])
    service = recover_service(config)
    try:
        refused = call(service, RECOVER, {})
        assert service.coordinator.status()["incident_stands"] is True
    finally:
        lock.release()
        other.close()
        close(service)

    assert refused["error_type"] == "resource_busy", refused
    assert refused["operator_command"] == recovery_operator_command(incident), refused
    assert ledger(config) == []


def test_a_recovery_whose_audit_cannot_be_written_keeps_the_incident_standing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_hardware: None) -> None:
    config = config_for(tmp_path)
    standing_incident(config, LEASE_RELEASE_RETRY_REASON)

    def no_ledger(*args: object, **kwargs: object) -> None:
        raise OSError("td6: the ledger refused the line")

    monkeypatch.setattr("agentic_hil.coordination.safe_append_text", no_ledger)
    service = recover_service(config)
    try:
        refused = call(service, RECOVER, {})
        assert service.coordinator.status()["incident_stands"] is True
    finally:
        close(service)

    assert refused["error_type"] == "recovery_audit_failed", refused
    assert refused["quarantined"] is True, refused


def test_a_recovery_that_could_not_persist_every_marker_keeps_the_incident_and_resumes_on_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_hardware: None) -> None:
    config = config_for(tmp_path)
    incident = standing_incident(config, LEASE_RELEASE_RETRY_REASON)
    service = recover_service(config)
    try:
        coordinator = service.coordinator
        monkeypatch.setattr(coordinator, "_write_record", failing_write_record(coordinator, lambda resource, record: record.get("state") == "released"))
        failed = call(service, RECOVER, {})
        assert failed["error_type"] == "recovery_persist_failed", failed
        assert failed["retry_safe"] is True, failed
        assert coordinator.status()["incident_stands"] is True

        restore_write_record(coordinator, monkeypatch)
        resumed = call(service, RECOVER, {})
    finally:
        close(service)

    assert resumed["ok"] is True, resumed
    assert resumed["recovered_quarantine_id"] == incident, resumed
    assert resumed["resumed"] is True, resumed


# server_upgrade


def test_upgrade_without_the_grant_answers_permission_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    never_runs(monkeypatch)
    service = AgenticHILToolService(upgradable_config(tmp_path, allow_upgrade=False), frontend="mcp")
    try:
        refused = call(service, UPGRADE, {})
    finally:
        close(service)

    assert refused["error_type"] == "permission_denied", refused
    assert refused["permission"] == "permissions.allow_upgrade", refused


def test_upgrade_on_windows_is_the_operators_command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert upgrade._host_locks_running_files() is (os.name == "nt")
    monkeypatch.setattr("agentic_hil.upgrade._host_locks_running_files", lambda: True)
    never_runs(monkeypatch)
    service = AgenticHILToolService(upgradable_config(tmp_path), frontend="mcp")
    try:
        refused = call(service, UPGRADE, {})
    finally:
        close(service)

    assert refused["error_type"] == "upgrade_cli_only_on_host", refused
    assert refused["upgrade_command"] == "agentic-hil upgrade", refused


def test_an_upgrade_on_disk_leaves_this_server_running_the_old_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_manager(monkeypatch, installed=subprocess.CompletedProcess([], 0, "installed\n", ""), version_after="9.9.9")
    service = AgenticHILToolService(upgradable_config(tmp_path), frontend="mcp")
    try:
        upgraded = call(service, UPGRADE, {})
    finally:
        close(service)

    assert upgraded["ok"] is True, upgraded
    assert upgraded["version"] == "9.9.9"
    assert upgraded["running_version"] == __version__
    assert upgraded["restart_required"] is True


def test_an_installation_already_on_the_newest_release_answers_already_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_manager(monkeypatch, installed=subprocess.CompletedProcess([], 0, "", "Nothing to upgrade\n"), version_after=__version__)
    service = AgenticHILToolService(upgradable_config(tmp_path), frontend="mcp")
    try:
        result = call(service, UPGRADE, {})
    finally:
        close(service)

    assert result["ok"] is True, result
    assert result["already_current"] is True, result
    assert result["restart_required"] is False, result
    assert result["running_version"] == result["version"] == __version__


def manager_answering(monkeypatch: pytest.MonkeyPatch, manager: subprocess.CompletedProcess[str], version: subprocess.CompletedProcess[str]) -> None:
    """fake_manager's stand-ins, with the version probe answering `version` whatever its exit code."""
    fake_manager(monkeypatch, installed=manager, version_after=__version__)

    def run(invoked: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return version if invoked[-1] == "--version" else manager

    monkeypatch.setattr("agentic_hil.upgrade._run_upgrade_process", run)


FAILED = subprocess.CompletedProcess([], 1, "", "error: the index answered 500\n")


@pytest.mark.parametrize(
    ("arrange", "error_type", "reinstall"),
    [
        pytest.param(lambda monkeypatch: manager_answering(monkeypatch, FAILED, subprocess.CompletedProcess([], 0, f"{__version__}\n", "")), "upgrade_failed", False, id="intact"),
        pytest.param(
            lambda monkeypatch: manager_answering(monkeypatch, FAILED, subprocess.CompletedProcess([], 0, "9.9.9\n", "")),
            "installation_changed_after_failed_upgrade",
            True,
            id="changed-on-disk",
        ),
        pytest.param(
            lambda monkeypatch: manager_answering(monkeypatch, FAILED, subprocess.CompletedProcess([], 1, "", "ModuleNotFoundError: No module named 'agentic_hil'\n")),
            "installation_broken",
            True,
            id="broken",
        ),
        pytest.param(
            lambda monkeypatch: fake_manager(monkeypatch, installed=subprocess.CompletedProcess([], 0, "", UV_EXACT_PIN_HINT), version_after=__version__),
            "upgrade_blocked_by_pin",
            True,
            id="pinned",
        ),
        pytest.param(lambda monkeypatch: a_manager_that_cannot_be_reached(monkeypatch, holders=[]), "upgrade_manager_not_found", False, id="no-manager"),
    ],
)
def test_a_failed_upgrade_keeps_this_server_on_its_release_and_names_the_reinstall_where_there_is_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arrange: Callable[[pytest.MonkeyPatch], None], error_type: str, reinstall: bool
) -> None:
    arrange(monkeypatch)
    service = AgenticHILToolService(upgradable_config(tmp_path), frontend="mcp")
    try:
        refused = call(service, UPGRADE, {})
    finally:
        close(service)

    assert refused["ok"] is False, refused
    assert refused["error_type"] == error_type, refused
    assert refused["running_version"] == __version__, refused
    assert ("reinstall_command" in refused) is reinstall, refused
    if error_type in {"installation_changed_after_failed_upgrade", "installation_broken"}:
        assert refused["restart_required"] is False, "a restart would load what the failed run left"


def test_every_process_an_upgrade_runs_is_bounded_at_six_hundred_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bound is per process: the manager run and the version probe after it each get their own."""
    real_run = subprocess.run
    bounded: list[tuple[list[str], object]] = []

    def run(command: Any, *args: Any, **kwargs: Any) -> Any:
        invoked = [str(part) for part in command] if isinstance(command, list) else []
        if invoked[:1] == ["uv.exe"] or invoked[-3:] == ["-m", "agentic_hil", "--version"]:
            bounded.append((invoked, kwargs.get("timeout")))
            output = "9.9.9\n" if invoked[-1] == "--version" else "installed\n"
            return subprocess.CompletedProcess(invoked, 0, output, "")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr("agentic_hil.upgrade._host_locks_running_files", lambda: False)
    monkeypatch.setattr("agentic_hil.upgrade._upgrade_command", lambda: ("uv", ["uv.exe", "tool", "upgrade", "agentic-hil"]))
    monkeypatch.setattr("agentic_hil.upgrade._processes_holding_installation", list)
    monkeypatch.setattr("agentic_hil.upgrade._installed_extras", lambda: ())
    monkeypatch.setattr(upgrade.subprocess, "run", run)
    service = AgenticHILToolService(upgradable_config(tmp_path), frontend="mcp")
    try:
        upgraded = call(service, UPGRADE, {})
    finally:
        close(service)

    assert upgraded["ok"] is True, upgraded
    assert len(bounded) >= 2, bounded
    assert any(command[:1] == ["uv.exe"] for command, _ in bounded), bounded
    assert {timeout for _, timeout in bounded} == {600}, bounded


@pytest.mark.parametrize(
    ("manager", "named", "tail"),
    [
        ("uv-tool", "uv", ["tool", "upgrade", "agentic-hil"]),
        ("uv-pip", "uv", ["pip", "install"]),
        ("pipx", "pipx", ["upgrade", "agentic-hil"]),
        ("pip", "pip", ["-m", "pip", "install", "--upgrade"]),
    ],
)
def test_the_upgrade_runs_the_manager_that_installed_this_copy(monkeypatch: pytest.MonkeyPatch, manager: str, named: str, tail: list[str]) -> None:
    monkeypatch.setattr("agentic_hil.upgrade.owning_manager", lambda: manager)
    monkeypatch.setattr("agentic_hil.upgrade.shutil.which", lambda name: f"/td6/bin/{name}")
    chosen, command = upgrade._upgrade_command()

    assert chosen == named
    assert command[1 : 1 + len(tail)] == tail, command
    if manager in {"uv-tool", "uv-pip", "pipx"}:
        assert command[0] == f"/td6/bin/{named}"
    else:
        assert command[0] == sys.executable


def test_an_installation_no_manager_owns_is_not_upgraded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentic_hil.upgrade.owning_manager", lambda: "unknown")
    with pytest.raises(ConfigError) as refused:
        upgrade._upgrade_command()
    assert refused.value.error_type == "upgrade_manager_not_established"


def test_upgrade_refuses_a_version_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    never_runs(monkeypatch)
    service = AgenticHILToolService(upgradable_config(tmp_path), frontend="mcp")
    try:
        refused = call(service, UPGRADE, {"version": "0.1.0"})
    finally:
        close(service)

    assert refused["error_type"] == "invalid_argument", refused
