"""Render a result document for the person who ran the command.

Every CLI frontend answers with one JSON document. That is the right answer for
the installer's step 4, for the evaluation harness, for the agent and for every
subprocess caller, and it is the wrong answer for the operator who typed
`agentic-hil init --force` and got a hundred lines of machine-readable detail
whose one actionable sentence sits in a `summary` field somewhere in the middle.

So the document is rendered rather than dumped whenever stdout is a terminal,
and printed byte for byte unchanged whenever it is not, or whenever `--json`
says a machine is reading after all. `sys.stdout.isatty()` is the whole of the
decision: a pipe, a redirect, a subprocess pipe and a captured stream all answer
false on every platform this runs on, so every existing caller is safe by
construction and needs no flag.

Nothing here decides anything and nothing here is a second source of truth. It
reformats the document it is handed, and a field a person would act on is never
dropped: the generic renderer lists what it does not recognise rather than
hiding it, which is why a command that registers no renderer of its own still
comes back as something readable rather than as a wall of braces.
"""
from __future__ import annotations

import re
import shutil
import textwrap
from collections.abc import Callable, Iterable, Mapping, Sequence

from agentic_hil.knowledge import (
    READ_VALIDATION_NEXT_STEP,
    command_line_remediation,
    remediation_fields,
)
from agentic_hil.report import conclusive_success
from agentic_hil.types import JsonObject

# What `--json` says on every `--help` page. One text, because the flag is added
# to the top-level parser and to every subcommand and the two must not drift.
# Every result is rendered for a person unless this says otherwise. Who is
# reading is declared rather than guessed: sniffing stdout got it wrong for
# every wrapper that captures the output in order to act on it and then shows
# a person what came back, which is what the installer does and what most
# things that run this command do.
JSON_FLAG_HELP = (
    "print the JSON document instead of the rendering. The document is the machine contract, field for field, "
    "and every caller that parses this command has to ask for it: a pipe, a redirect and a subprocess are "
    "rendered like anything else"
)

# The two commands that own stdout for a protocol rather than for a result. They
# never reach the renderer, and the guard is here rather than in the entrypoint's
# head so that the one list lives beside the one decision.
PROTOCOL_COMMANDS = frozenset({"mcp-stdio", "com-stdio"})

_INDENT = "  "
_MIN_WIDTH = 60
_MAX_WIDTH = 100
_FALLBACK_WIDTH = 88
# Fields whose place in the rendering is fixed, so the generic pass over
# "everything else" must not print them a second time.
_HANDLED_EVERYWHERE = frozenset({"ok", "tool", "summary", "next_step", "next_steps", "remediation", "do_not", "warnings", "error_type", "meaning"})

# The names a captured subprocess carries its own words under, wherever the
# document nests them. A result that ran a package manager, a debugger or a
# build keeps the tool's output here, and the tool wrote it in lines: reflowing
# it into a key/value row turns a stack of `error:` lines into one unreadable
# strip, which is the same as not printing it.
_PROCESS_OUTPUT_KEYS = frozenset({"stdout", "stderr"})
# How much captured output a report prints before it says it is cutting. A
# manager that fails writes a handful of lines and a build that fails writes
# thousands, and the line that names the cause sits at either end of them, so
# the cut is taken out of the middle and both ends survive it. Nothing is ever
# dropped without the rendering saying how much.
_LITERAL_MAX_LINES = 40
_LITERAL_HEAD_LINES = 20
_LITERAL_TAIL_LINES = 15
_LITERAL_MAX_LINE_CHARS = 500


def render_result(result: JsonObject, command: str | None = None) -> str:
    """The document, rendered for a person, ending in exactly one newline."""
    lines = _render_lines(result, command)
    body = "\n".join(_tidy(lines))
    return f"{body}\n" if body else ""


def write_rendered(stream: object, text: str) -> None:
    """Write the rendering, and never fail on a stream that cannot spell it.

    The machine document is ASCII whatever it holds, because `json.dumps`
    escapes: the JSON path has never had an encoding to get wrong. Prose does,
    and this project's own texts carry punctuation a legacy console codepage or
    a C locale cannot encode, so a result that printed perfectly as JSON would
    have died with a `UnicodeEncodeError` the moment it was rendered. The
    substitute characters cost a dash; failing costs the answer.
    """
    write = stream.write  # type: ignore[attr-defined]
    try:
        write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        write(text.encode(encoding, "replace").decode(encoding, "replace"))


def _render_lines(result: JsonObject, command: str | None) -> list[str]:
    result = _audit_errors_where_they_happened(result)
    result = _one_error_type_answered_once(result)
    if _error_type(result):
        # A refusal is rendered the same way for every command: the error type
        # names it, the summary says what happened, and the catalogue says what
        # to do about it. A per-command renderer has nothing to add to that and
        # several would have quietly dropped the remediation list.
        #
        # The command is handed on all the same. It is not what the refusal is
        # rendered *by*; it is the one fact this layer holds about who is
        # reading, and for a refusal that has a route for each reader the
        # catalogue puts theirs first.
        return render_refusal(result, command)
    renderer = _RENDERERS.get(command or "")
    return renderer(result) if renderer is not None else render_generic(result)


# ---------------------------------------------------------------------------
# Building blocks.


def _width() -> int:
    columns = shutil.get_terminal_size(fallback=(_FALLBACK_WIDTH, 24)).columns
    return max(_MIN_WIDTH, min(_MAX_WIDTH, columns - 2))


def _flat(text: object) -> str:
    return " ".join(str(text).split())


def _wrap(text: object, indent: str = "", hanging: str | None = None) -> list[str]:
    """One paragraph, wrapped to the terminal, never breaking a path or a command."""
    body = _flat(text)
    if not body:
        return []
    return textwrap.wrap(
        body,
        width=_width(),
        initial_indent=indent,
        subsequent_indent=indent if hanging is None else hanging,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [indent + body]


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "not set"
    if isinstance(value, (list, tuple)):
        return ", ".join(_scalar(item) for item in value)
    return _flat(value)


def _renderable_scalar(value: object) -> bool:
    """Whether a value belongs in a key/value row rather than in a block."""
    if isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple)):
        return bool(value) and all(isinstance(item, (str, int, float, bool)) for item in value)
    return False


def _fields(pairs: Sequence[tuple[str, object]], indent: str = _INDENT) -> list[str]:
    """An aligned key/value block, skipping every pair that has nothing to say."""
    rows = [(label, _scalar(value)) for label, value in pairs if value is not None and _scalar(value) != ""]
    if not rows:
        return []
    label_width = max(len(label) for label, _ in rows)
    lines: list[str] = []
    for label, value in rows:
        head = f"{indent}{label.ljust(label_width)}  "
        lines.extend(_wrap(value, indent=head, hanging=" " * len(head)))
    return lines


def _process_output(key: object, value: object) -> str:
    """The captured output this member holds, or "" when it is not one.

    Keyed on the name rather than on the shape, because that is what a caller
    reading the document goes by too, and a multi-line string under any other
    name is prose the wrapper is right about.
    """
    if not isinstance(key, str) or key not in _PROCESS_OUTPUT_KEYS:
        return ""
    return value if isinstance(value, str) and value.strip() else ""


def _clip_line(line: str) -> str:
    """One captured line, and how much of it was left out when it is enormous."""
    if len(line) <= _LITERAL_MAX_LINE_CHARS:
        return line
    return f"{line[:_LITERAL_MAX_LINE_CHARS]} [... {len(line) - _LITERAL_MAX_LINE_CHARS} characters not shown ...]"


def _literal_block(text: str, indent: str) -> list[str]:
    """Captured output, printed the way the process wrote it.

    Indented and otherwise untouched: no wrapping, no reflowing, no quoting. The
    whole point of this block is that the operator reads their own machine's
    error in their own machine's words, and every transformation this module
    applies to prose is a transformation that would move them. `splitlines`
    rather than `split("\\n")` so a progress bar written with carriage returns
    comes apart into lines instead of arriving as one.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) > _LITERAL_MAX_LINES:
        cut = len(lines) - _LITERAL_HEAD_LINES - _LITERAL_TAIL_LINES
        lines = [*lines[:_LITERAL_HEAD_LINES], f"[... {cut} lines not shown ...]", *lines[-_LITERAL_TAIL_LINES:]]
    return [f"{indent}{_clip_line(line)}" for line in lines]


def _members(value: Mapping[str, object]) -> tuple[list[tuple[str, object]], list[tuple[str, object]]]:
    """An object's members, split into the rows and the things that need a body.

    One split, used by every pass over an object, so a nested object and a
    captured stream are placed the same way at every depth and in every section.
    """
    rows: list[tuple[str, object]] = []
    bodies: list[tuple[str, object]] = []
    for key, item in _one_audit_error(value).items():
        if _renderable_scalar(item) and not _process_output(key, item):
            rows.append((key, item))
        else:
            bodies.append((str(key), item))
    return rows, bodies


# The two fields an audit failure is carried in. `audit_error` is `audit_errors[0]`
# wherever one is built (`report.merge_audit_status`, `report.mark_audit_failure`,
# `test_reactor.propagate_result_status`): one field for a caller that reads a single
# failure, one list for a caller that reads them all, and the same object in both.
_AUDIT_ERROR_KEYS = ("audit_error", "audit_errors")


def _one_audit_error(value: Mapping[str, object]) -> Mapping[str, object]:
    """The same object's two names collapsed to the one that carries them all.

    The document is right to hold both, and a person reading one refusal was
    shown the identical paragraph and the identical five remediation items twice
    in a row for it (#387). The list is what survives, because it is the one that
    is complete when a run failed its audit in more than one place.
    """
    errors = _entries(value.get("audit_errors"))
    if not errors or _mapping(value.get("audit_error")) not in errors:
        return value
    return {key: item for key, item in value.items() if key != "audit_error"}


def _audit_errors_where_they_happened(result: JsonObject) -> JsonObject:
    """Drop an aggregate's copy of the audit failures its own steps already carry.

    A plan that cannot write its audit trail answers with the same refusal three
    times: in the failing step's result, in the top-level `audit_error`, and in
    the top-level `audit_errors` the aggregate flattens every step's into. All
    three belong in the document, which is a machine contract read field by
    field. None of them is one a person needs twice, and the copies furthest
    from the step stand furthest from the one thing that explains them.

    Only what is provably the same object goes, compared by value against
    everything else this document holds. An audit failure no step carries, a
    cleanup that could not be recorded, a run that failed before its first step,
    is printed nowhere else and stays exactly where it is.
    """
    errors = _entries(result.get("audit_errors"))
    if not errors:
        return result
    elsewhere = {key: value for key, value in result.items() if key not in _AUDIT_ERROR_KEYS}
    return elsewhere if all(_holds(elsewhere, error) for error in errors) else result


def _one_error_type_answered_once(result: JsonObject) -> JsonObject:
    """Drop a nested finding's error type where it is the enclosing refusal's own.

    A refused test plan carries the reason twice by design: `error_type` at the
    top and the finding's own `error_type` inside `validation_error`, so a
    caller reading either field gets one answer. Both derive their advice from
    the same catalogue entry, so a rendering that asked twice printed the
    identical numbered list and the identical `do_not` block one under the
    other, which is the #387 defect in a second place.

    Only a copy is dropped, and only the one furthest from what explains it: the
    headline already names the type, `step_error_type` repeats it in the rows,
    and a nested finding whose type differs from the enclosing refusal's is a
    second fact and keeps its own advice. Nothing is taken out of the document,
    which is rendered from a copy.
    """
    error_type = _error_type(result)
    nested = result.get("validation_error")
    if not error_type or not isinstance(nested, Mapping) or nested.get("error_type") != error_type:
        return result
    if _strings(nested.get("remediation")) or _strings(nested.get("do_not")):
        return result
    return {**result, "validation_error": {key: value for key, value in nested.items() if key != "error_type"}}


def _holds(node: object, needle: JsonObject) -> bool:
    """Whether ``needle`` already stands somewhere inside ``node``, by value."""
    if isinstance(node, Mapping):
        return dict(node) == needle or any(_holds(item, needle) for item in node.values())
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        return any(_holds(item, needle) for item in node)
    return False


def _member_lines(key: str, value: object, indent: str) -> list[str]:
    """One member that does not fit on a row, under its own key.

    Captured output becomes a literal block, a nested object is rendered by the
    same rules one level in, a list is unfolded. A member with nothing to say
    contributes nothing: a heading with nothing under it says less than no
    heading at all, and is what a structure this pass could not render produced.
    """
    captured = _process_output(key, value)
    if captured:
        return [f"{indent}{key}", *_literal_block(captured, indent + _INDENT)]
    if isinstance(value, Mapping) and value:
        body = _nested_mapping(value, indent + _INDENT)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and value and not _renderable_scalar(value):
        body = _nested_sequence(value, indent + _INDENT)
    else:
        return []
    return [f"{indent}{key}", *body] if body else []


def _bullets(items: Iterable[object], indent: str = _INDENT) -> list[str]:
    lines: list[str] = []
    for item in items:
        lines.extend(_wrap(_scalar(item), indent=f"{indent}- ", hanging=f"{indent}  "))
    return lines


def _numbered(items: Sequence[object], indent: str = _INDENT) -> list[str]:
    lines: list[str] = []
    width = len(str(len(items)))
    for number, item in enumerate(items, start=1):
        head = f"{indent}{str(number).rjust(width)}. "
        lines.extend(_wrap(_scalar(item), indent=head, hanging=" " * len(head)))
    return lines


def _section(title: str, body: Sequence[str]) -> list[str]:
    return ["", title, *body] if body else []


def _tidy(lines: Iterable[str]) -> list[str]:
    tidied: list[str] = []
    for line in lines:
        stripped = line.rstrip()
        if not stripped and (not tidied or not tidied[-1]):
            continue
        tidied.append(stripped)
    while tidied and not tidied[-1]:
        tidied.pop()
    return tidied


def _mapping(value: object) -> JsonObject:
    return dict(value) if isinstance(value, Mapping) else {}


def _entries(value: object) -> list[JsonObject]:
    return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []


def _named(entry: Mapping[str, object], *names: str) -> object:
    """The first of these keys the entry actually carries, or None.

    For a rendering that has to read a producer's field under more than one
    spelling. `in` rather than a chain of `get` defaults, because a key that is
    present and holds `None` is the absent value it says it is rather than a
    reason to fall through to the next name.
    """
    return next((entry[name] for name in names if name in entry), None)


def _strings(value: object) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_flat(item) for item in value if isinstance(item, (str, int, float))]
    return []


def _error_type(result: Mapping[str, object]) -> str:
    value = result.get("error_type")
    return value.strip() if isinstance(value, str) and value.strip() and result.get("ok") is not True else ""


def _summary(result: Mapping[str, object]) -> str:
    value = result.get("summary")
    return _flat(value) if isinstance(value, str) and value.strip() else ""


def _verdict(result: Mapping[str, object]) -> str:
    """The one word a person reads first when the summary does not say it."""
    return "" if result.get("ok") is True else "Failed."


# The markers that make the exit-code verdict false over a result whose own `ok`
# is true: a call that reached the hardware and could not say what it left
# behind. `complete` is not one of them and was: an enumeration that states how
# far it reaches says something about the enumeration rather than about this run
# of it, the summary already says it in words, and scoring it here put "did not
# come back clean" over a discovery that worked (#445).
_CONTAINMENT_MARKERS = ("cleanup_required", "quarantined", "quarantine_id", "audit_ok", "target_ok", "cleanup_ok", "side_effect_status", "hardware_state", "lease_state")


def _containment(result: JsonObject) -> list[str]:
    """Name a result that says `ok` and is not clean, because the exit code will.

    `conclusive_success` is the verdict the exit code is taken from, and it is not
    `ok` alone: a quarantine, an audit that could not be written, or an effect
    this bench cannot account for. A rendering that printed only the summary
    would show a person a calm sentence beside an exit code of 1, which is the
    one disagreement this layer must never introduce.
    """
    if result.get("ok") is not True or conclusive_success(dict(result)):
        return []
    rows = [(marker, result[marker]) for marker in _CONTAINMENT_MARKERS if marker in result]
    body = _wrap("This did not come back clean, and the exit code says so. What is standing:", indent=_INDENT)
    return ["", *body, *_fields(rows, indent=_INDENT * 2)]


def _headline(result: JsonObject) -> list[str]:
    """The summary as prose, and the containment markers right under it.

    Every renderer opens with this, so the one thing a person must not read past
    is in the same place in all of them.
    """
    lines = _wrap(_summary(result) or _verdict(result))
    lines.extend(_containment(result))
    return lines


def _tail(result: JsonObject) -> list[str]:
    """Warnings, then the next step, in that order and at the end, always."""
    lines: list[str] = []
    lines.extend(_section("Warnings", _bullets(_strings(result.get("warnings")))))
    steps = _strings(result.get("next_steps"))
    if steps:
        lines.extend(_section("Next steps", _numbered(steps)))
    next_step = result.get("next_step")
    if isinstance(next_step, str) and next_step.strip():
        lines.extend(_section("Next step", _wrap(next_step, indent=_INDENT)))
    return lines


# The catalogue is scoped by whichever of these a refusal names, and which one
# that is depends on who raised it: a config field, the backend that answered,
# the adapter, the permission that is closed. Tried in turn, and an unscoped
# entry answers when none of them has one, which is exactly what `lookup_remedy`
# already does. One tuple, because the lookup and the command-line ordering have
# to ask about the same entry or they answer out of two.
_REMEDY_SCOPE_KEYS = ("field", "backend", "adapter", "permission", "tool")

# What a person at a shell types to do what a tool does. One catalogue serves
# both surfaces, so a step reading "call debugger_probes_list" is right for the
# agent and names something that does not exist in an operator's shell. The
# document is untouched: this is the rendering saying the same move in the
# reader's own vocabulary, and only where the two are the same move with the
# same inputs. A command with arguments the tool does not take, or a tool with
# arguments the command does not take, is not an entry here.
_CLI_COMMANDS = {
    "debugger_probes_list": "agentic-hil debugger-probes",
    "project_config_adopt_hardware": "agentic-hil adopt-hardware",
    "server_upgrade": "agentic-hil upgrade",
    "test_reactor_run": "agentic-hil test-reactor",
    "test_reactor_status": "agentic-hil test-reactor-status",
    "test_reactor_stop": "agentic-hil test-reactor-stop",
}

# Every other tool this project's remediation names, and none of them is
# rewritten. Three reasons, and each name here has one of them:
#
# * the hardware and session tools, the report readers and `bench_run_*`: an
#   agent's vocabulary with no command line behind it at all. A step naming one
#   is telling an agent what to do next, and there is nothing else to call it.
# * `project_config_set`, `project_config_describe` and
#   `project_config_reload_description`: the command line has `grant`, `revoke`
#   and `config-reload`, and not one of them is the same move. `grant` writes
#   one named permission, `config-reload` reports what a running server would
#   take from the file and reloads nothing.
# * `project_config_create` and `hardware_recover`: a command exists
#   (`agentic-hil init`, `agentic-hil recover`) and the catalogue already names
#   it where it means it. `config_file_not_found` carries one ordering per
#   reader and the shell reader's opens with `agentic-hil init`;
#   `unsafe_configured_path` names both spellings in one sentence on purpose;
#   and the `hardware_recover` step is about `operator_statement`, which is a
#   tool argument the command does not have.
#
# The guard over this file reads both collections together: a tool the catalogue
# starts recommending has to be put in one of them, so nobody has to notice.
_TOOLS_KEEPING_THEIR_NAME = frozenset({
    "bench_run_start",
    "bench_run_status",
    "bench_run_stop",
    "can_buses_list",
    "can_read",
    "can_send",
    "can_session_stop",
    "classify_last_error",
    "com_session_stop",
    "debug_continue",
    "debug_dump_symbol_ihex",
    "debug_get_stop_reason",
    "debug_stop_session",
    "debug_symbol_info",
    "debug_symbol_value",
    "debugger_info",
    "flash_firmware",
    "get_last_report",
    "hardware_recover",
    "probe_target",
    "project_config_create",
    "project_config_describe",
    "project_config_reload_description",
    "project_config_set",
    "reset_target",
})

# The tool name as it is written in prose, with or without the backticks the
# catalogue puts around it, and never as part of a longer identifier.
_TOOL_MENTION = re.compile(r"`?\b(" + "|".join(sorted(_CLI_COMMANDS)) + r")\b`?")


def _in_the_readers_words(step: str) -> str:
    """One remediation step, with every tool name it recommends spelled as the
    command that does it.

    A step that names the MCP surface is left exactly as it is. Those steps are
    not advice a reader translates: they say which surface the move is on, and
    `config_file_not_found` carries one for each reader in the same entry, so
    rewriting the agent's step into the operator's command would leave a
    sentence that says "over MCP" about a shell command.
    """
    return step if "MCP" in step else _TOOL_MENTION.sub(lambda match: f"`{_CLI_COMMANDS[match.group(1)]}`", step)


def _for_the_reader(steps: Sequence[str], command_line: bool) -> list[str]:
    return [_in_the_readers_words(step) for step in steps] if command_line else list(steps)


def _command_line_order(result: JsonObject, steps: Sequence[str]) -> list[str]:
    """The catalogue's ordering for a person at a shell, where it has one.

    Same scope chain the lookup below uses, and the unscoped entry last, because
    `lookup_remedy` falls back to it and a scoped entry is the more specific
    answer where one exists. The catalogue decides whether anything moves; this
    only says which reader is holding the page.
    """
    error_type = _error_type(result) or None
    scopes: list[str | None] = [value for key in _REMEDY_SCOPE_KEYS if isinstance(value := result.get(key), str)]
    for scope in [*scopes, None]:
        ordered = command_line_remediation(error_type, scope, list(steps))
        if ordered is not None:
            return ordered
    return list(steps)


def _remediation(result: JsonObject, *, command_line: bool = False) -> tuple[list[str], list[str]]:
    """What to do and what not to do, from the result or from the catalogue.

    A refusal built by `ConfigError.to_dict` already carries both, merged from
    the same catalogue the MCP reference serves. A refusal built by hand -- a
    `permission_denied` from a tool surface, say -- carries neither, and the
    catalogue still knows the answer, so it is looked up rather than left out.

    `command_line` says a person typed a command to get here, and it decides two
    things: which of the catalogue's two orderings is printed, for a refusal
    whose readers have different first moves, and whether a tool this project
    also ships a command for is named as that command. It changes no step and
    drops none: `command_line_remediation` reorders the entry's own steps or
    declines, and the rewriting is one name for another name for one move.

    The rewriting is last, after the ordering, because the ordering is the
    catalogue's own and is matched against the catalogue's own text: a step
    already translated would no longer be one of the entry's, and
    `command_line_remediation` would decline to order a list it could not
    recognise.
    """
    steps = _strings(result.get("remediation"))
    avoid = _strings(result.get("do_not"))
    if steps or avoid:
        ordered = _command_line_order(result, steps) if command_line else steps
        return _for_the_reader(ordered, command_line), _for_the_reader(avoid, command_line)
    error_type = _error_type(result) or None
    # The one substitution the catalogue cannot make for itself: which permission
    # a refusal is about is a fact about this refusal. Passed in so the advice
    # names the key the reader has to move rather than the generic shape of one
    # (#443), and so an entry written around that key declines to answer for a
    # `permission_denied` that names none.
    permission = result.get("permission")
    for key in _REMEDY_SCOPE_KEYS:
        scope = result.get(key)
        catalogue = remediation_fields(
            error_type,
            scope if isinstance(scope, str) else None,
            permission=permission if isinstance(permission, str) and permission else None,
        )
        if catalogue:
            found = _strings(catalogue.get("remediation"))
            ordered = _command_line_order(result, found) if command_line else found
            return _for_the_reader(ordered, command_line), _for_the_reader(_strings(catalogue.get("do_not")), command_line)
    return [], []


# ---------------------------------------------------------------------------
# The refusal, and the generic fallback every command gets for free.


def _work_was_done(result: Mapping[str, object]) -> bool:
    """Whether this document is about a call that actually ran something.

    A refusal is a statement that nothing happened: no step ran, nothing on the
    bench moved, and the fix is the caller's before there is anything to read
    about the hardware. A plan that flashed the board, opened the port, reset it,
    read it and then did not meet its comparator is the opposite of that, and it
    arrives here through the same door because it is `ok: false` and carries an
    `error_type`. Heading it `Refused:` puts the word that means "your setup is
    wrong" over the deliberate red run a starter project ships.

    Two fields answer it, and both are about what the run did rather than about
    what it concluded: a non-empty `steps` list, which the reactor writes one
    record into per executed step and leaves empty for every plan refused at
    preflight, and `side_effect_committed`, which a single call sets when it has
    left something behind. `failed_step` is deliberately not one of them: a plan
    refused at preflight carries it too, naming the step whose *text* is wrong,
    so reading it as evidence of execution would call every schema error a failed
    test run.
    """
    if result.get("side_effect_committed") is True:
        return True
    steps = result.get("steps")
    return isinstance(steps, Sequence) and not isinstance(steps, (str, bytes)) and bool(steps)


def render_refusal(result: JsonObject, command: str | None = None) -> list[str]:
    """A refusal, for whoever is reading it.

    `command` is the subcommand a person typed, so its presence is what says a
    person is reading rather than a caller parsing the document. It decides
    nothing about the refusal itself; the only thing it reaches is which of the
    catalogue's orderings the steps are printed in, for the refusals that carry
    one for each reader.

    The heading is the run's own outcome word where the run had one. Everything
    under it is identical either way: the same summary, the same details, the
    same remediation, in the same order. Only the first word moves, because it
    is the one a reader takes the whole document's meaning from.
    """
    error_type = _error_type(result)
    lines = [f"{'Failed' if _work_was_done(result) else 'Refused'}: {error_type}", ""]
    lines.extend(_wrap(_summary(result) or "The command was refused.", indent=_INDENT))
    meaning = result.get("meaning")
    if isinstance(meaning, str) and meaning.strip():
        lines.append("")
        lines.extend(_wrap(meaning, indent=_INDENT))
    lines.extend(_section("Details", _refusal_details(result)))
    steps, avoid = _remediation(result, command_line=command is not None)
    steps = _without_absent_pointers(result, steps)
    lines.extend(_section("What to do", _numbered(steps)))
    lines.extend(_section("Do not", _bullets(avoid)))
    lines.extend(_tail(result))
    return lines


def _without_absent_pointers(result: JsonObject, steps: Sequence[str]) -> list[str]:
    """Drop advice that points at a field this refusal does not carry.

    One step, exactly: `test_config_invalid` opens by sending the reader to
    `validation_error.next_step`, which is the right first move for the refusal
    that has one and an instruction to read nothing for the refusal that has
    not. A plan refused for a session it never opened carries no such field, and
    the line printed all the same.

    This is the one thing a renderer may take out of a refusal, and it is not a
    change to what the refusal says: the step is a pointer at a field, and the
    field is not there. Everything else the catalogue writes is printed as
    written, which is why the sentence is matched against the catalogue's own
    constant rather than against a copy kept here.
    """
    validation_error = result.get("validation_error")
    pointed_at = validation_error.get("next_step") if isinstance(validation_error, Mapping) else None
    if isinstance(pointed_at, str) and pointed_at.strip():
        return list(steps)
    return [step for step in steps if step != READ_VALIDATION_NEXT_STEP]


def _refusal_details(result: JsonObject) -> list[str]:
    """Everything the refusal carries that its own place has not already taken.

    A refusal that ran something carries the diagnosis in a nested object rather
    than in a field: `upgrade_failed` puts the manager's returncode and the
    manager's own words under `install`, and a Details section that printed only
    scalars showed an operator every fact about the failure except the one that
    says why it happened, and sent them back to `--json` to read their own
    machine's error. So the nested objects are rendered under their key and the
    captured streams are printed as the process wrote them. It stays a report:
    rows first, then the objects that need a body, and never a dump of braces.
    """
    rows, bodies = _members({key: value for key, value in result.items() if key not in _HANDLED_EVERYWHERE})
    lines = _fields(rows)
    for key, value in bodies:
        lines.extend(_member_lines(key, value, _INDENT))
    return lines


def render_generic(result: JsonObject) -> list[str]:
    """Something dignified for a command that registered no renderer of its own.

    The summary as prose, then the scalar fields the document carries, then the
    lists and the nested results it carries, then the next step. It never dumps
    JSON: a nested object is rendered by the same rules one level in, however
    deep it goes, and captured process output is printed as a block rather than
    reflowed, so nothing is silently dropped and nothing is unreadable.
    """
    lines = _headline(result)
    if not lines and result.get("ok") is True:
        lines.append("OK.")
    scalars: list[tuple[str, object]] = []
    blocks: list[str] = []
    for key, value in result.items():
        if key in _HANDLED_EVERYWHERE:
            continue
        captured = _process_output(key, value)
        if captured:
            blocks.extend(_section(key, _literal_block(captured, _INDENT)))
        elif _renderable_scalar(value):
            scalars.append((key, value))
        elif isinstance(value, Mapping):
            blocks.extend(_section(key, _nested_mapping(value)))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and value:
            blocks.extend(_section(key, _nested_sequence(value)))
    if scalars:
        lines.append("")
        lines.extend(_fields(scalars))
    lines.extend(blocks)
    lines.extend(_tail(result))
    return lines


def _nested_mapping(value: Mapping[str, object], indent: str = _INDENT) -> list[str]:
    """A nested object: its own summary if it has one, else its members.

    Its rows first, then whatever needs a body, one indent further in, by the
    same rules and to whatever depth the object goes. A `verification` inside an
    `install` inside a refusal is a real shape this project builds, and stopping
    the walk at a fixed depth would have dropped it without saying so.
    """
    if _looks_like_result(value):
        return _result_lines(value, indent)
    rows, bodies = _members(value)
    lines = _fields(rows, indent=indent)
    for key, item in bodies:
        lines.extend(_member_lines(key, item, indent))
    return lines


def _nested_sequence(value: Sequence[object], indent: str = _INDENT) -> list[str]:
    if all(isinstance(item, (str, int, float, bool)) for item in value):
        return _bullets(value, indent=indent)
    lines: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            # A result-shaped entry is a result wherever it stands, so it is
            # rendered by the same pass a nested one is: its summary heads the
            # bullet, `error_type` and the numbered remediation land where every
            # other refusal keeps them. Anything else keeps the older rule, where
            # the first scalar field carries the bullet, because a list of small
            # objects is nearly always a list of named things and a bare `-` with
            # the name on the line under it reads as an empty entry.
            #
            # A list of refusals is what made the distinction worth drawing.
            # `ConfigError.to_dict` opens with `ok: False`, so `audit_errors`
            # rendered its first entry as `- no` and then folded five numbered
            # remediation items into one comma-joined row (#387).
            if _looks_like_result(item):
                lines.extend(_wrap(_result_head(item), indent=f"{indent}- ", hanging=f"{indent}  "))
                lines.extend(_result_body(item, indent + _INDENT * 2))
                continue
            rows, bodies = _members(item)
            if rows:
                lines.extend(_wrap(_scalar(rows[0][1]), indent=f"{indent}- ", hanging=f"{indent}  "))
                lines.extend(_fields(rows[1:], indent=indent + _INDENT * 2))
            else:
                lines.append(f"{indent}-")
            for key, sub in bodies:
                lines.extend(_member_lines(key, sub, indent + _INDENT * 2))
        else:
            lines.extend(_bullets([item], indent=indent))
    return lines


def _looks_like_result(value: Mapping[str, object]) -> bool:
    return "summary" in value or "ok" in value


def _result_head(value: Mapping[str, object]) -> str:
    """The one sentence a nested result is introduced by, wherever it stands."""
    return _summary(value) or _verdict(value) or "ok"


def _result_lines(value: Mapping[str, object], indent: str = _INDENT) -> list[str]:
    """A nested result: its verdict, its summary, its own fields, its remediation.

    The fields come with it rather than being summarised away, and so does what
    it nests under them. A nested result is where `config_status` publishes the
    digest an operator compares against a running server, and where a step that
    ran something keeps what it ran and what came back; a rendering that kept
    only the sentence would have been the one place this layer lost something
    somebody acts on. The advice stays last, under the facts it is advice about.
    """
    return [*_wrap(_result_head(value), indent=indent), *_result_body(value, indent)]


def _result_body(value: Mapping[str, object], indent: str) -> list[str]:
    """Everything a nested result carries under its own opening sentence.

    Its `next_step` included, which is where the specific answer lives. A plan
    refused for naming a COM port this bench does not have carries the exact
    `agentic-hil adopt-hardware` line under `validation_error.next_step`, and
    the rendering printed every other field of that object and dropped that one,
    while the advice above it opened by telling the reader to go and read it
    (#446). `_tail` prints the same field at the top level; this is the same
    rule one level in, and it stands under the facts for the same reason.
    """
    error_type = _error_type(value)
    lines = _fields([("error_type", error_type)], indent=indent) if error_type else []
    rows, bodies = _members({key: item for key, item in value.items() if key not in _HANDLED_EVERYWHERE})
    lines.extend(_fields(rows, indent=indent))
    for key, item in bodies:
        lines.extend(_member_lines(key, item, indent))
    next_step = value.get("next_step")
    if isinstance(next_step, str) and next_step.strip():
        lines.extend(_wrap(next_step, indent=indent))
    if error_type:
        steps, avoid = _remediation(dict(value))
        lines.extend(_numbered(steps, indent=indent + _INDENT))
        lines.extend(_bullets([f"do not: {item}" for item in avoid], indent=indent + _INDENT))
    return lines


# ---------------------------------------------------------------------------
# Steps, the shape half these commands answer in.


def _step_label(name: str) -> str:
    return name.replace("_", " ")


def _step_verdict(step: Mapping[str, object]) -> str:
    """One word per step, taken from the document and never invented.

    `skipped` and `ok` are two facts rather than one: a step the run deliberately
    left out is `skipped` with `ok` true, and a step the run never got to is
    `skipped` with `ok` false, which counts against the whole but is not a
    failure of that step. Calling both of them FAILED sent people debugging the
    step after the one that actually broke.
    """
    skipped = step.get("skipped") is True
    if step.get("ok") is True:
        return "skipped" if skipped else "ok"
    return "not reached" if skipped else "FAILED"


def _steps_block(steps: Mapping[str, object]) -> list[str]:
    """One line per step, plus the failure detail of any step that failed.

    The step name and its verdict are aligned so a person reads the column and
    stops at the one that is not `ok`; the summary follows on the same line and
    wraps under it.
    """
    rows = [(name, dict(value)) for name, value in steps.items() if isinstance(value, Mapping)]
    if not rows:
        return []
    label_width = max(len(_step_label(name)) for name, _ in rows)
    verdict_width = max(len(_step_verdict(step)) for _, step in rows)
    lines: list[str] = []
    for name, step in rows:
        verdict = _step_verdict(step)
        head = f"{_INDENT}{_step_label(name).ljust(label_width)}  {verdict.ljust(verdict_width)}  "
        lines.extend(_wrap(_summary(step) or verdict, indent=head, hanging=" " * len(head)))
        error_type = _error_type(step)
        if error_type:
            detail = " " * len(head)
            lines.extend(_fields([("error_type", error_type)], indent=detail))
            remedy, avoid = _remediation(step)
            lines.extend(_numbered(remedy, indent=detail))
            lines.extend(_bullets([f"do not: {item}" for item in avoid], indent=detail))
    return lines


def _rollback_line(result: JsonObject) -> list[str]:
    rollback = _mapping(result.get("rollback"))
    if not rollback:
        return []
    if rollback.get("attempted") is not True:
        return []
    errors = _strings(rollback.get("errors"))
    if errors:
        return _section("Rollback", ["  This command's own file changes could not all be put back:", *_bullets(errors, indent=_INDENT * 2)])
    return _section("Rollback", _wrap("Every file this command had written was put back the way it found it.", indent=_INDENT))


def _permission_changes(result: JsonObject) -> list[str]:
    return _section("Permission changes", _bullets(_strings(result.get("permission_changes"))))


def _state_root_changes(result: JsonObject) -> list[str]:
    return _section("State root changes", _bullets(_strings(result.get("state_root_changes")))) if _strings(result.get("state_root_changes")) else []


def _restart_lines(result: JsonObject) -> list[str]:
    """What is left to restart, and which processes are waiting for it.

    The sentence first, then the processes it is about. `restart_required_by` is
    what an upgrade puts there: the servers that were started out of the
    installation before it was replaced and go on answering with the old release
    until their host restarts. They are rendered the way every other list of
    named things is, pid on the bullet and image under it, because the operator
    reads them to decide which window to close.
    """
    body: list[str] = []
    notice = result.get("restart_notice")
    if isinstance(notice, str) and notice.strip():
        # `_with_restart_notice` appends this sentence to the summary as well, so
        # that a caller reading only `summary` still gets it. On one screen that
        # is the same paragraph twice, which reads as two different notices.
        if _flat(notice) not in _summary(result):
            body.extend(_wrap(notice, indent=_INDENT))
    elif result.get("restart_required") is True:
        body.extend(_wrap("Restart the agent host before it uses this registration.", indent=_INDENT))
    waiting = _entries(result.get("restart_required_by"))
    if waiting:
        body.extend(_nested_sequence(waiting, indent=_INDENT))
    return _section("Restart", body)


def _left_behind(result: JsonObject) -> list[str]:
    entry = _mapping(result.get("left_behind"))
    if not entry:
        return []
    return _section("Left behind", _fields([(key, value) for key, value in entry.items() if _renderable_scalar(value)]))


# ---------------------------------------------------------------------------
# The commands a person actually meets.


def render_init(result: JsonObject) -> list[str]:
    lines = _headline(result)
    lines.append("")
    lines.extend(
        _fields(
            [
                ("config_path", result.get("config_path")),
                ("agent", result.get("agent") or "none named"),
                ("scope", result.get("scope")),
            ]
        )
    )
    lines.extend(_section("Steps", _steps_block(_mapping(result.get("steps")))))
    lines.extend(_state_root_changes(result))
    lines.extend(_permission_changes(result))
    lines.extend(_rollback_line(result))
    lines.extend(_tail(result))
    return lines


def render_setup(result: JsonObject) -> list[str]:
    lines = _headline(result)
    scopes = _mapping(result.get("scopes"))
    user = _mapping(scopes.get("user"))
    project = _mapping(scopes.get("project"))
    lines.append("")
    lines.extend(
        _fields(
            [
                ("agent", result.get("agent")),
                ("command", user.get("command")),
                ("config_path", project.get("config_path")),
            ]
        )
    )
    if user or project:
        lines.extend(
            _section(
                "Halves",
                _steps_block(
                    {
                        "user (agent-install)": user or {"ok": False, "summary": "not reached"},
                        "project (init)": project or {"ok": False, "summary": "not reached"},
                    }
                ),
            )
        )
    lines.extend(_section("Steps", _steps_block(_mapping(result.get("steps")))))
    lines.extend(_restart_lines(result))
    lines.extend(_state_root_changes(result))
    lines.extend(_permission_changes(result))
    lines.extend(_rollback_line(result))
    lines.extend(_left_behind(result))
    lines.extend(_tail(result))
    return lines


def render_agent_install(result: JsonObject) -> list[str]:
    lines = _headline(result)
    lines.append("")
    lines.extend(_fields([("agent", result.get("agent")), ("scope", result.get("scope")), ("command", result.get("command"))]))
    lines.extend(_section("Steps", _steps_block(_mapping(result.get("steps")))))
    lines.extend(_restart_lines(result))
    lines.extend(_permission_changes(result))
    lines.extend(_rollback_line(result))
    lines.extend(_left_behind(result))
    lines.extend(_tail(result))
    return lines


def render_upgrade(result: JsonObject) -> list[str]:
    lines = _headline(result)
    lines.append("")
    lines.extend(
        _fields(
            [
                ("previous_version", result.get("previous_version")),
                ("version", result.get("version")),
                ("manager", result.get("manager")),
                ("command", result.get("command")),
                ("upgraded_on_disk", result.get("upgraded_on_disk")),
            ]
        )
    )
    refreshed = _entries(result.get("refreshed"))
    if refreshed:
        lines.extend(_section("Agent integrations refreshed", _steps_block({str(entry.get("agent", "agent")): entry for entry in refreshed})))
        for entry in refreshed:
            command = entry.get("command")
            if entry.get("ok") is not True and command:
                lines.extend(_fields([(f"finish {entry.get('agent', 'agent')} by hand", _scalar(command))], indent=_INDENT * 2))
    extras = _mapping(result.get("extras_warning"))
    if extras:
        lines.extend(_section("Extras", [*_wrap(_summary(extras), indent=_INDENT), *_fields([("reinstall_command", extras.get("reinstall_command"))])]))
    superseded = _strings(result.get("superseded_entrypoints"))
    if superseded:
        lines.extend(
            _section(
                "Superseded launchers",
                [
                    *_wrap(
                        "Renamed aside so the new copy could land while a running process still had the old image mapped. "
                        "A later upgrade deletes them once nothing maps them any more; nothing needs doing about them here.",
                        indent=_INDENT,
                    ),
                    *_bullets(superseded),
                ],
            )
        )
    lines.extend(_restart_lines(result))
    lines.extend(_tail(result))
    return lines


def render_uninstall(result: JsonObject) -> list[str]:
    lines = _headline(result)
    lines.extend(_section("Removed", _bullets([f"{entry.get('what', 'item')}: {entry.get('path', '')}" for entry in _entries(result.get("removed"))]) or _wrap("Nothing of this installation's own was found here.", indent=_INDENT)))
    left_alone = _entries(result.get("left_alone"))
    if left_alone:
        body: list[str] = []
        for entry in left_alone:
            body.extend(_bullets([f"{entry.get('what', 'item')}: {entry.get('path', '')}"]))
            body.extend(_wrap(entry.get("reason", ""), indent=_INDENT * 2))
        lines.extend(_section("Left alone, because this installation did not write it", body))
    kept = _entries(result.get("kept"))
    if kept:
        body = []
        for entry in kept:
            body.extend(_bullets([f"{entry.get('what', 'tree')}: {entry.get('path', '')}"]))
            body.extend(_wrap(entry.get("reason", entry.get("why", "")), indent=_INDENT * 2))
        lines.extend(_section("Kept on purpose, and no flag purges them", body))
    agents = _entries(result.get("agents"))
    failed = [entry for entry in agents if entry.get("ok") is not True]
    if failed:
        lines.extend(_section("Not fully taken back", _steps_block({str(entry.get("agent", "agent")): entry for entry in failed})))
    removal = _mapping(result.get("package_removal"))
    if removal:
        lines.extend(_section("The package itself", _fields([("manager", removal.get("manager")), ("command", removal.get("command")), ("package_directory", removal.get("package_directory"))])))
    lines.extend(_tail(result))
    return lines


def render_doctor(result: JsonObject) -> list[str]:
    """The health check the documentation sends people to, as a report.

    Section by section, in the order somebody diagnosing a bench needs them: the
    verdict, the configuration this run was decided by, the installation the
    hardware gate is actually running out of, what to register as the MCP
    server, then every configured device with its permissions and its checks.
    """
    lines = _headline(result)
    status = _mapping(result.get("config_status"))
    lines.append("")
    lines.extend(
        _fields(
            [
                ("config_path", result.get("config_path")),
                ("state", status.get("state")),
                ("loaded_digest", status.get("loaded_digest")),
                ("reload_required", status.get("reload_required")),
            ]
        )
    )
    if _summary(status) and status.get("state") not in {None, "current"}:
        lines.extend(_wrap(_summary(status), indent=_INDENT))

    # Beside the configuration and before anything about devices, because this is
    # the root the audit trail, the leases and the reports are written under: a
    # bench whose state root the enforcer refuses cannot record a hardware action
    # at all, whatever every device below says about itself.
    state_root = _mapping(result.get("state_root"))
    if state_root:
        verdict = "ok" if state_root.get("ok") is True else "FAILED"
        body = _fields([("path", state_root.get("path")), ("verdict", verdict)])
        body.extend(_wrap(_summary(state_root), indent=_INDENT))
        error_type = _error_type(state_root)
        if error_type:
            body.extend(_fields([("error_type", error_type)], indent=_INDENT))
            resolved = state_root.get("resolved_parent")
            if isinstance(resolved, str) and resolved:
                body.extend(_fields([("resolved_parent", resolved)], indent=_INDENT))
            remedy, avoid = _remediation(dict(state_root))
            body.extend(_numbered(remedy, indent=_INDENT * 2))
            body.extend(_bullets([f"do not: {item}" for item in avoid], indent=_INDENT * 2))
        lines.extend(_section("State root", body))

    # Immediately after it, and before the devices, because it answers the same
    # class of question one level up: the state root says whether a hardware
    # action can be recorded, and this says whether there is any hardware to
    # record one about. A reader who gets to the device sections without having
    # been told the bench is unbound reads a list of entries as a bench.
    binding = _mapping(result.get("bench_binding"))
    if binding:
        verdict = "ok" if binding.get("ok") is True else "UNBOUND"
        body = _fields([("verdict", verdict)])
        body.extend(_wrap(_summary(binding), indent=_INDENT))
        for entry in _entries(binding.get("unbound")):
            body.extend(_fields([(str(entry.get("field", "field")), ", ".join(_strings(entry.get("entries"))) or "-")], indent=_INDENT))
            body.extend(_wrap(_summary(entry), indent=_INDENT * 2))
        next_step = binding.get("next_step")
        if isinstance(next_step, str) and next_step:
            body.extend(_numbered([next_step], indent=_INDENT))
        lines.extend(_section("Bench binding", body))

    installation = _mapping(result.get("installation"))
    if installation:
        body = _fields([("version", installation.get("version")), ("package_path", installation.get("package_path")), ("editable", installation.get("editable"))])
        if _summary(installation):
            body.extend(_wrap(_summary(installation), indent=_INDENT))
        lines.extend(_section("Installation", body))

    mcp = _mapping(result.get("mcp"))
    if mcp:
        body = _fields([("transport", mcp.get("transport")), ("command", mcp.get("command")), ("args", mcp.get("args")), ("persistent", mcp.get("persistent"))])
        if _summary(mcp):
            body.extend(_wrap(_summary(mcp), indent=_INDENT))
        rejected = _entries(mcp.get("rejected_candidates"))
        if rejected:
            body.append(f"{_INDENT}rejected candidates")
            for entry in rejected:
                body.extend(_fields([(str(entry.get("path", "candidate")), entry.get("reason", entry.get("summary", "")))], indent=_INDENT * 2))
        lines.extend(_section("MCP registration", body))

    target = _mapping(result.get("target"))
    if target:
        lines.extend(_section("Target", _fields([("name", target.get("name")), ("controller", target.get("controller"))])))

    debuggers = _mapping(result.get("debuggers"))
    lines.extend(_section("Debuggers", _doctor_debuggers(debuggers)))
    # The standalone debugger report repeats the bound entry's own check and is
    # left out when it does. It is the only place the skipped case says anything
    # at all -- no configured debugger pins a toolchain, so nothing was checked
    # and the sentence naming `executable` is the whole finding -- and that one
    # is the case a person is most likely to be reading `doctor` about.
    if not any("check" in _mapping(entry) for entry in debuggers.values()):
        standalone = _mapping(result.get("debugger"))
        if _summary(standalone):
            lines.extend(_section("Debugger check", _wrap(_summary(standalone), indent=_INDENT)))
    lines.extend(_section("COM ports", _doctor_devices(_mapping(result.get("com_ports")), ("device", "baudrate", "encoding", "serial_number", "identity_source", "resource_id"))))
    lines.extend(_section("CAN buses", _doctor_devices(_mapping(result.get("can_buses")), ("adapter", "channel", "bitrate", "fd"))))

    extras = _mapping(result.get("missing_extras"))
    if extras:
        lines.extend(_section("Missing extras", [*_wrap(_summary(extras), indent=_INDENT), *_fields([("reinstall_command", extras.get("reinstall_command"))])]))
    lines.extend(_section("Warnings", _bullets(_strings(result.get("warnings")))))
    comparison = status.get("compare_with_running_server")
    if isinstance(comparison, str) and comparison.strip():
        lines.extend(_section("Compared with a running server", _wrap(comparison, indent=_INDENT)))
    next_step = result.get("next_step")
    if isinstance(next_step, str) and next_step.strip():
        lines.extend(_section("Next step", _wrap(next_step, indent=_INDENT)))
    return lines


def _permission_line(permissions: Mapping[str, object]) -> str:
    granted = sorted(name for name, value in permissions.items() if value is True)
    closed = sorted(name for name, value in permissions.items() if value is False)
    parts = [f"granted: {', '.join(granted)}" if granted else "granted: none"]
    if closed:
        parts.append(f"closed: {', '.join(closed)}")
    return "; ".join(parts)


def _script_line(entry: Mapping[str, object]) -> str:
    """One OpenOCD script field, saying what kind of name it is.

    The report keeps `value`, `kind` and either `exists` or a note apart on
    purpose: `interface/stlink.cfg` names no file on this host and is not meant
    to, and a rendering that showed only a missing path would recreate exactly
    the misreading that report exists to prevent.
    """
    parts = [_scalar(entry.get("value"))]
    kind = entry.get("kind")
    if kind:
        parts.append(f"({_scalar(kind)})")
    if entry.get("resolved_by"):
        parts.append(f"resolved by {_scalar(entry['resolved_by'])}")
    elif "exists" in entry:
        parts.append("present on this host" if entry.get("exists") is True else "NOT present on this host")
    return " ".join(parts)


def _check_verdict(check: Mapping[str, object]) -> str:
    """A check's own word for itself, and the verdict only when it has none.

    `target_support` answers `supported`, `unsupported`, `undetermined` or
    `not_configured`, and the difference between the last two and a failure is
    the difference between a broken bench and a host that could not answer the
    question. Flattening them to FAILED is what would make `doctor` read red
    over a machine that simply has no toolchain installed yet.
    """
    status = check.get("status")
    if isinstance(status, str) and status.strip():
        return _flat(status)
    if check.get("skipped") is True:
        return "skipped"
    return "ok" if check.get("ok") is True else "FAILED"


def _doctor_debuggers(debuggers: Mapping[str, object]) -> list[str]:
    if not debuggers:
        return _wrap("None configured.", indent=_INDENT)
    lines: list[str] = []
    for name, raw in debuggers.items():
        entry = _mapping(raw)
        marks = [str(entry.get("type") or "unknown type")]
        marks.append("bound" if entry.get("bound") is True else "not bound")
        lines.append(f"{_INDENT}{name} ({', '.join(marks)})")
        detail = _INDENT * 2
        # `probe_id` is named even when it is unset: a bench with more than one
        # probe attached and no serial pinned is the case this report exists for.
        rows: list[tuple[str, object]] = [("probe_id", entry.get("probe_id") or "not set")]
        for key, value in _mapping(entry.get("scripts")).items():
            rows.append((key, _script_line(_mapping(value)) if isinstance(value, Mapping) else value))
        permissions = _mapping(entry.get("permissions"))
        if permissions:
            rows.append(("permissions", _permission_line(permissions)))
        lines.extend(_fields(rows, indent=detail))
        checks = [(label, _mapping(entry.get(label))) for label in ("check", "target_support") if _mapping(entry.get(label))]
        if not checks:
            continue
        label_width = max(len(label) for label, _ in checks)
        verdict_width = max(len(_check_verdict(check)) for _, check in checks)
        for label, check in checks:
            head = f"{detail}{label.ljust(label_width)}  {_check_verdict(check).ljust(verdict_width)}  "
            lines.extend(_wrap(_summary(check) or check.get("undetermined_reason") or _check_verdict(check), indent=head, hanging=" " * len(head)))
            error_type = _error_type(check)
            if error_type:
                lines.extend(_fields([("error_type", error_type)], indent=" " * len(head)))
                remedy, _ = _remediation(check)
                lines.extend(_numbered(remedy, indent=" " * len(head)))
    return lines


def _doctor_devices(devices: Mapping[str, object], keys: Sequence[str]) -> list[str]:
    if not devices:
        return _wrap("None configured.", indent=_INDENT)
    lines: list[str] = []
    for name, raw in devices.items():
        entry = _mapping(raw)
        lines.append(f"{_INDENT}{name}")
        detail = _INDENT * 2
        lines.extend(_fields([(key, entry.get(key)) for key in keys if entry.get(key) is not None], indent=detail))
        permissions = _mapping(entry.get("permissions"))
        if permissions:
            lines.extend(_fields([("permissions", _permission_line(permissions))], indent=detail))
    return lines


def render_recover(result: JsonObject) -> list[str]:
    lines = _headline(result)
    lines.append("")
    lines.extend(
        _fields(
            [
                ("recovered_quarantine_id", result.get("recovered_quarantine_id")),
                ("was_quarantined", result.get("was_quarantined")),
                ("nothing_to_recover", result.get("nothing_to_recover")),
                ("actor", result.get("actor")),
                ("attestation", result.get("attestation")),
                ("resumed", result.get("resumed")),
                ("config_change_accepted", result.get("config_change_accepted")),
                ("resolved_reason", result.get("resolved_reason")),
                ("operator_statement", result.get("operator_statement")),
            ]
        )
    )
    lines.extend(_section("Resources released", _bullets(_strings(result.get("resources")))))
    lines.extend(_section("Cleanup reasons this bench still carries", _bullets(_strings(result.get("cleanup_reasons")))))
    lines.extend(_tail(result))
    return lines


def render_adopt_hardware(result: JsonObject) -> list[str]:
    lines = _headline(result)
    lines.append("")
    lines.extend(
        _fields(
            [
                ("path", result.get("path")),
                ("debugger_id", result.get("debugger_id")),
                ("com_port_id", result.get("com_port_id")),
                ("applied", result.get("applied")),
                ("reload_required", result.get("reload_required")),
            ]
        )
    )
    carried = _entries(result.get("carried"))
    if carried:
        title = "Filled in" if result.get("applied") is True else "Would be filled in"
        lines.extend(_section(title, _fields([(str(item.get("key", "key")), item.get("value")) for item in carried])))
    already = _entries(result.get("already_current"))
    if already:
        lines.extend(_section("Already match the attached hardware", _bullets([str(item.get("key", "key")) for item in already])))
    kept = _entries(result.get("kept"))
    if kept:
        # The names `adopt.py` writes are `configured_value` and
        # `discovered_value`, and this read them under two spellings it never
        # writes: every comparison a person came here to see rendered as
        # "configured not set, attached not set" while the same plan's JSON
        # carried both values, which told an operator a key was empty while it
        # held the value that decided the comparison (#442). The older spellings
        # stay in the lookup so a document written by another producer still
        # renders its values rather than the same two blanks.
        #
        # The verdict is on the row as well as in the heading above it. "Left
        # alone" says a decision was taken; which of the two values survives it
        # is the thing the operator has to act on, and the entry's own `reason`
        # says why that one, so it is printed rather than dropped.
        body: list[str] = []
        for item in kept:
            configured = _scalar(_named(item, "configured_value", "configured", "value"))
            discovered = _scalar(_named(item, "discovered_value", "discovered", "attached"))
            body.extend(_fields([(str(item.get("key", "key")), f"configured {configured}, attached {discovered}, adoption keeps {configured}")]))
            body.extend(_wrap(item.get("reason", ""), indent=_INDENT * 2))
            body.extend(_wrap(item.get("next_step", ""), indent=_INDENT * 2))
        lines.extend(_section("Left alone, because somebody set them", body))
    unavailable = _entries(result.get("unavailable"))
    if unavailable:
        body = []
        for item in unavailable:
            body.extend(_bullets([f"{item.get('key', 'key')}: {_summary(item) or item.get('reason', 'not answerable from what is attached')}"]))
        lines.extend(_section("Could not be answered from what is attached", body))
    created = _strings(result.get("created_entries"))
    if created:
        lines.extend(_section("Entries created", _bullets(created)))
    lines.extend(_section("Permissions changed", _bullets(_strings(result.get("permissions_changed")))))
    lines.extend(_tail(result))
    return lines


def render_debugger_probes(result: JsonObject) -> list[str]:
    lines = _headline(result)
    # `discovered_by` beside the backend, because on an OpenOCD bench the two are
    # not the same fact: the backend is the toolchain the entry drives, and the
    # enumeration that produced these ids is this host's USB serial inventory
    # rather than anything OpenOCD ran. A reader deciding whether a serial was
    # read off the probe or off the descriptor it published needs that on screen.
    #
    # `complete` with them, as one more thing this listing says about itself. It
    # used to reach a person only through the containment block, which printed it
    # under "this did not come back clean" beside an exit code of 1; the exit code
    # no longer says that and the field still has to be on screen, because how far
    # an enumeration reaches is exactly what a reader counting probes needs (#445).
    header = _fields([
        ("source", result.get("source")),
        ("backend", result.get("backend")),
        ("discovered_by", result.get("discovered_by")),
        ("complete", result.get("complete")),
        ("quarantine_id", result.get("quarantine_id")),
    ])
    if header:
        lines.append("")
        lines.extend(header)
    probes = _entries(result.get("probes"))
    if probes:
        lines.extend(_section("Probes", _bullets([", ".join(f"{key} {_scalar(value)}" for key, value in probe.items() if _renderable_scalar(value)) for probe in probes])))
    per_debugger = _mapping(result.get("debuggers"))
    for name, raw in per_debugger.items():
        entry = _mapping(raw)
        found = _entries(entry.get("probes"))
        body = _wrap(_summary(entry) or _verdict(entry), indent=_INDENT)
        body.extend(_bullets([", ".join(f"{key} {_scalar(value)}" for key, value in probe.items() if _renderable_scalar(value)) for probe in found], indent=_INDENT * 2))
        error_type = _error_type(entry)
        if error_type:
            body.extend(_fields([("error_type", error_type)], indent=_INDENT * 2))
            remedy, _ = _remediation(entry)
            body.extend(_numbered(remedy, indent=_INDENT * 2))
        lines.extend(_section(name, body))
    configured = _strings(result.get("configured_debuggers"))
    if configured:
        lines.extend(_section("Configured debuggers", _bullets(configured)))
    lines.extend(_tail(result))
    return lines


# Keyed on the subcommand, which is what the entrypoint already knows and what a
# person typed. A command with no entry here gets `render_generic`, which is
# required to be readable on its own; these exist because the commands a person
# meets first deserve better than readable.
_RENDERERS: dict[str, Callable[[JsonObject], list[str]]] = {
    "init": render_init,
    "setup": render_setup,
    "agent-install": render_agent_install,
    "upgrade": render_upgrade,
    "uninstall": render_uninstall,
    "doctor": render_doctor,
    "recover": render_recover,
    "adopt-hardware": render_adopt_hardware,
    "debugger-probes": render_debugger_probes,
}
