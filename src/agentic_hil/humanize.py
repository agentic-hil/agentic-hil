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

import shutil
import sys
import textwrap
from collections.abc import Callable, Iterable, Mapping, Sequence

from agentic_hil.knowledge import remediation_fields
from agentic_hil.report import overall_success
from agentic_hil.types import JsonObject

# What `--json` says on every `--help` page. One text, because the flag is added
# to the top-level parser and to every subcommand and the two must not drift.
JSON_FLAG_HELP = (
    "print the JSON document instead of the human-readable rendering. The document is what an agent, a script or "
    "the installer reads, and it is already what is printed whenever stdout is not a terminal, so a pipe or a "
    "redirect needs no flag"
)

# The other direction, which the tty rule alone cannot reach. A frontend that
# runs this CLI to show a person the answer has to capture stdout to act on the
# result at all, and capturing is exactly what the tty rule reads as "a machine
# is on the other end". install.sh is that frontend: it captures the report to
# decide whether the step succeeded, and printed the raw document at the person
# it exists to serve on the one path where the document is all they get.
HUMAN_FLAG_HELP = (
    "render the result for a person even when stdout is not a terminal. For a frontend that captures this "
    "command's output and shows it to somebody; --json wins if both are given, and mcp-stdio and com-stdio "
    "are unaffected because their stdout carries a protocol"
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


def stdout_is_terminal() -> bool:
    """Whether a person is watching stdout right now.

    Everything POSIX-only stays out of this on purpose: Windows PowerShell is a
    first-class host here, and `sys.stdout.isatty()` answers on every platform.
    A stream that has been closed or replaced by something without the method is
    not a terminal, which is the safe answer in both directions: it prints the
    machine document, which is what every non-interactive caller expects.
    """
    isatty = getattr(sys.stdout, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (ValueError, OSError):
        return False


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
    if _error_type(result):
        # A refusal is rendered the same way for every command: the error type
        # names it, the summary says what happened, and the catalogue says what
        # to do about it. A per-command renderer has nothing to add to that and
        # several would have quietly dropped the remediation list.
        return render_refusal(result)
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


# The markers that make `overall_success` false over a result whose own `ok` is
# true: a call that reached the hardware and could not say what it left behind.
_CONTAINMENT_MARKERS = ("cleanup_required", "quarantined", "quarantine_id", "audit_ok", "target_ok", "cleanup_ok", "side_effect_status", "hardware_state", "lease_state")


def _containment(result: JsonObject) -> list[str]:
    """Name a result that says `ok` and is not clean, because the exit code will.

    `overall_success` is the verdict the exit code is taken from, and it is not
    `ok` alone: a quarantine, an audit that could not be written, an effect this
    bench cannot account for. A rendering that printed only the summary would
    show a person a calm sentence beside an exit code of 1, which is the one
    disagreement this layer must never introduce.
    """
    if result.get("ok") is not True or overall_success(dict(result)):
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


def _remediation(result: JsonObject) -> tuple[list[str], list[str]]:
    """What to do and what not to do, from the result or from the catalogue.

    A refusal built by `ConfigError.to_dict` already carries both, merged from
    the same catalogue the MCP reference serves. A refusal built by hand -- a
    `permission_denied` from a tool surface, say -- carries neither, and the
    catalogue still knows the answer, so it is looked up rather than left out.
    """
    steps = _strings(result.get("remediation"))
    avoid = _strings(result.get("do_not"))
    if steps or avoid:
        return steps, avoid
    error_type = _error_type(result) or None
    # The catalogue is scoped by whichever of these the refusal names, and which
    # one that is depends on who raised it: a config field, the backend that
    # answered, the adapter, the permission that is closed. Tried in turn, and an
    # unscoped entry answers when none of them has one, which is exactly what
    # `lookup_remedy` already does.
    for key in ("field", "backend", "adapter", "permission", "tool"):
        scope = result.get(key)
        catalogue = remediation_fields(error_type, scope if isinstance(scope, str) else None)
        if catalogue:
            return _strings(catalogue.get("remediation")), _strings(catalogue.get("do_not"))
    return [], []


# ---------------------------------------------------------------------------
# The refusal, and the generic fallback every command gets for free.


def render_refusal(result: JsonObject) -> list[str]:
    error_type = _error_type(result)
    lines = [f"Refused: {error_type}", ""]
    lines.extend(_wrap(_summary(result) or "The command was refused.", indent=_INDENT))
    meaning = result.get("meaning")
    if isinstance(meaning, str) and meaning.strip():
        lines.append("")
        lines.extend(_wrap(meaning, indent=_INDENT))
    details = _fields([(key, value) for key, value in result.items() if key not in _HANDLED_EVERYWHERE and _renderable_scalar(value)])
    lines.extend(_section("Details", details))
    steps, avoid = _remediation(result)
    lines.extend(_section("What to do", _numbered(steps)))
    lines.extend(_section("Do not", _bullets(avoid)))
    lines.extend(_tail(result))
    return lines


def render_generic(result: JsonObject) -> list[str]:
    """Something dignified for a command that registered no renderer of its own.

    The summary as prose, then the scalar fields the document carries, then the
    lists and the nested results it carries, then the next step. It never dumps
    JSON: a nested object is rendered by the same rules one level in, and an
    object too deep to render that way is reported by name and count rather than
    printed, so nothing is silently dropped and nothing is unreadable.
    """
    lines = _headline(result)
    if not lines and result.get("ok") is True:
        lines.append("OK.")
    scalars: list[tuple[str, object]] = []
    blocks: list[str] = []
    for key, value in result.items():
        if key in _HANDLED_EVERYWHERE:
            continue
        if _renderable_scalar(value):
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
    """One level of a nested object: its own summary if it has one, else its scalars."""
    if _looks_like_result(value):
        return _result_lines(value, indent)
    lines = _fields([(key, item) for key, item in value.items() if _renderable_scalar(item)], indent=indent)
    for key, item in value.items():
        body: list[str] = []
        if isinstance(item, Mapping) and item:
            body = _nested_mapping(item, indent + _INDENT) if _looks_like_result(item) else _fields([(name, sub) for name, sub in item.items() if _renderable_scalar(sub)], indent=indent + _INDENT)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and item and not _renderable_scalar(item):
            body = _nested_sequence(item, indent + _INDENT)
        # A heading with nothing under it says less than no heading at all, and
        # is what a structure nested deeper than this pass renders produced.
        if body:
            lines.append(f"{indent}{key}")
            lines.extend(body)
    return lines


def _nested_sequence(value: Sequence[object], indent: str = _INDENT) -> list[str]:
    if all(isinstance(item, (str, int, float, bool)) for item in value):
        return _bullets(value, indent=indent)
    lines: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            # The first scalar field carries the bullet, because a list of small
            # objects is nearly always a list of named things and a bare `-` with
            # the name on the line under it reads as an empty entry.
            rows = [(key, sub) for key, sub in item.items() if _renderable_scalar(sub)]
            if rows:
                lines.extend(_wrap(_scalar(rows[0][1]), indent=f"{indent}- ", hanging=f"{indent}  "))
                lines.extend(_fields(rows[1:], indent=indent + _INDENT * 2))
            else:
                lines.append(f"{indent}-")
            lines.extend(_nested_mapping({key: sub for key, sub in item.items() if not _renderable_scalar(sub)}, indent + _INDENT * 2))
        else:
            lines.extend(_bullets([item], indent=indent))
    return lines


def _looks_like_result(value: Mapping[str, object]) -> bool:
    return "summary" in value or "ok" in value


def _result_lines(value: Mapping[str, object], indent: str = _INDENT) -> list[str]:
    """A nested result: its verdict, its summary, its own fields, its remediation.

    The fields come with it rather than being summarised away. A nested result
    is where `config_status` publishes the digest an operator compares against a
    running server, and a rendering that kept only the sentence would have been
    the one place this layer lost something somebody acts on.
    """
    lines = _wrap(_summary(value) or _verdict(value) or "ok", indent=indent)
    error_type = _error_type(value)
    if error_type:
        lines.extend(_fields([("error_type", error_type)], indent=indent))
    lines.extend(_fields([(key, item) for key, item in value.items() if key not in _HANDLED_EVERYWHERE and _renderable_scalar(item)], indent=indent))
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
    notice = result.get("restart_notice")
    if isinstance(notice, str) and notice.strip():
        # `_with_restart_notice` appends this sentence to the summary as well, so
        # that a caller reading only `summary` still gets it. On one screen that
        # is the same paragraph twice, which reads as two different notices.
        if _flat(notice) in _summary(result):
            return []
        return _section("Restart", _wrap(notice, indent=_INDENT))
    if result.get("restart_required") is True:
        return _section("Restart", _wrap("Restart the agent host before it uses this registration.", indent=_INDENT))
    return []


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
        body: list[str] = []
        for item in kept:
            body.extend(_fields([(str(item.get("key", "key")), f"configured {_scalar(item.get('configured', item.get('value')))}, attached {_scalar(item.get('discovered', item.get('attached')))}")]))
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
    header = _fields([("source", result.get("source")), ("backend", result.get("backend")), ("quarantine_id", result.get("quarantine_id"))])
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
