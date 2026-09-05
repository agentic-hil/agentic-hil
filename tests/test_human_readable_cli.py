"""The command line answers a person in prose and a machine in JSON.

Every frontend produces one result document. That document is the contract the
installer's step 4 reads, the evaluation harness parses and every subprocess
caller in this suite decodes, and it is the wrong thing to put in front of the
operator who typed `agentic-hil init --force` and wanted one sentence.

So every result is rendered, and the document is what `--json` asks for. Who is
reading used to be guessed from `isatty`, and that guess was wrong about every
caller that captures this command in order to act on the result and then puts
what came back in front of a person: the installer's step 4 is exactly that, and
it was handing operators the machine document on the one path where the report is
all they get. A caller that parses says `--json` now, and nothing else has to say
anything.

The machine half is still the half that must not move, and the tests at the top
of this file pin it byte for byte through the print layer, under the flag rather
than under a pipe. The tests below them are about the other half, and their
standard is not "it printed something": it is that a field a person would act on
-- the next step, a warning, a permission that changed, a refusal's remediation
-- survives the rendering.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
from pathlib import Path

import pytest

from agentic_hil import __version__, cli
from agentic_hil.humanize import (
    JSON_FLAG_HELP,
    PROTOCOL_COMMANDS,
    render_result,
    write_rendered,
)
from agentic_hil.knowledge import remediation_fields

INIT_FORCED = {
    "ok": True,
    "tool": "agentic_hil_init",
    "scope": "project",
    "summary": "Agentic HIL project configured.",
    "agent": "claude-code",
    "config_path": "/home/op/.config/agentic-hil/projects/blinky/config.yaml",
    "state_root_changes": [],
    "permission_changes": ["tightened /home/op/.local/bin"],
    "rollback": {"attempted": False, "ok": True, "errors": []},
    "steps": {
        "config": {"ok": True, "summary": "Authoritative config written, every permission granted but the two flashing is interlocked against.", "forced": True},
        "doctor": {"ok": True, "tool": "agentic_hil_doctor", "summary": "Agentic HIL configuration loaded and 1 debugger(s) checked."},
        "agent_write_restriction": {"ok": True, "summary": "Claude Code was asked to refuse its own write tools."},
    },
    "warnings": ["This debugger names no probe_id."],
    "next_step": "Restart Claude Code so it loads the MCP server.",
}

INIT_FAILED = {
    "ok": False,
    "tool": "agentic_hil_init",
    "scope": "project",
    "summary": "Agentic HIL project setup failed; its own committed file changes were rolled back.",
    "agent": "codex",
    "config_path": "/home/op/.config/agentic-hil/projects/blinky/config.yaml",
    "state_root_changes": [],
    "permission_changes": [],
    "rollback": {"attempted": True, "ok": True, "errors": []},
    "steps": {
        "config": {"ok": True, "summary": "Authoritative config written."},
        "doctor": {"ok": False, "tool": "agentic_hil_doctor", "error_type": "config_stale", "summary": "The configuration changed under this run."},
        "agent_write_restriction": {"ok": False, "skipped": True, "summary": "Agent write restriction was not reached."},
    },
}

REFUSAL = {
    "ok": False,
    "error_type": "config_file_not_found",
    "summary": "No authoritative Agentic HIL configuration was found for this workspace.",
    "field": "workspace_root",
    "remediation": ["Run `agentic-hil setup --agent <agent>` from this project root.", "Or point AGENTIC_HIL_CONFIG at its absolute path."],
    "do_not": ["Do not write the configuration into the repository."],
}

DOCTOR = {
    "ok": False,
    "tool": "agentic_hil_doctor",
    "summary": "Agentic HIL configuration loaded, but the debugger check failed for: dut.",
    "config_status": {
        "state": "unchanged",
        "path": "/home/op/.config/agentic-hil/projects/blinky/config.yaml",
        "loaded_digest": "sha256:44be3c",
        "reload_required": False,
        "summary": "The file on disk is the one this command read.",
        "compare_with_running_server": "Compare the loaded_digest here with config_status.loaded_digest in that server's debugger_info.",
    },
    "config_path": "/home/op/.config/agentic-hil/projects/blinky/config.yaml",
    # The real version, read rather than spelled: a release number written into
    # a fixture is a number that goes stale, and `tools/check_version_consistency.py`
    # reads this tree for exactly that.
    "installation": {"version": __version__, "package_path": "/opt/agentic_hil", "editable": False},
    "mcp": {"transport": "stdio", "args": ["mcp-stdio"], "command": "/home/op/.local/bin/agentic-hil", "persistent": True, "summary": "Register this exact path."},
    "target": {"name": "nucleo_f446re", "controller": "stm32f446re"},
    "debuggers": {
        "dut": {
            "type": "openocd",
            "probe_id": None,
            "bound": True,
            "permissions": {"allow_flash": True, "allow_mass_erase": False},
            "scripts": {"interface_cfg": {"value": "interface/stlink.cfg", "kind": "search_name", "resolved_by": "openocd"}},
            "check": {"ok": False, "tool": "debugger_info", "error_type": "debugger_not_found", "summary": "OpenOCD was not found on this host."},
            "target_support": {"ok": True, "status": "undetermined", "undetermined_reason": "the debugger check did not complete."},
        }
    },
    "com_ports": {"dut_uart": {"device": "/dev/ttyACM0", "baudrate": 115200, "permissions": {"allow_write": True}}},
    "can_buses": {},
    "warnings": ["com_ports.dut_uart is identified by a device name the host can reassign."],
    "debugger": {"ok": False, "tool": "debugger_info", "summary": "OpenOCD was not found on this host."},
}

# The transcript from issue #314, field for field: `uv tool upgrade` came back
# non-zero, `_failed_upgrade` put `_process_result`'s three fields on the refusal
# under `install`, and the operator was shown every fact about the failure except
# the manager's own words for why it happened.
UPGRADE_FAILED = {
    "ok": False,
    "tool": "agentic_hil_upgrade",
    "manager": "uv",
    "command": ["/home/op/.local/bin/uv", "tool", "upgrade", "agentic-hil"],
    "python": "/home/op/.local/share/uv/tools/agentic-hil/bin/python3",
    "previous_version": "0.16.0",
    "install": {
        "returncode": 2,
        "stderr": "error: Failed to install agentic-hil\n  Caused by: Failed to fetch `https://pypi.org/simple/agentic-hil/`\n  Caused by: Request failed after 3 retries",
    },
    "error_type": "upgrade_failed",
    "summary": "Agentic HIL package manager reported an upgrade failure. This installation still runs 0.16.0 and was not replaced.",
    "installation_intact": True,
    "version": "0.16.0",
    "restart_required": False,
    "verification": {"returncode": 0, "stdout": "0.16.0"},
}

GENERIC = {
    "ok": True,
    "tool": "hardware_lease_status",
    "project_resource": "project:blinky",
    "owner_active": False,
    "incident_stands": False,
    "cleanup_reasons": [],
    "summary": "Nothing on this bench is held and nothing is standing.",
    "next_step": "Nothing to do.",
}


class FakeStdout(io.StringIO):
    """A stream that answers `isatty` the way a test needs it to."""

    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        # Through the real implementation first, so a closed stream raises here
        # exactly as a closed stdout does and the guard is what is under test.
        super().isatty()
        return self._tty


def _run(monkeypatch: pytest.MonkeyPatch, argv: list[str], document: object, *, tty: bool) -> tuple[int, str]:
    """Drive the entrypoint over a fixed document, so only the print layer varies."""
    monkeypatch.setattr(cli, "dispatch", lambda args: document)
    stream = FakeStdout(tty=tty)
    monkeypatch.setattr("sys.stdout", stream)
    code = cli.entrypoint(argv)
    return code, stream.getvalue()


# ---------------------------------------------------------------------------
# The machine contract, which is the half that may not move.


def test_the_json_flag_prints_exactly_the_document(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out = _run(monkeypatch, ["init", "--json"], INIT_FORCED, tty=False)
    assert code == 0
    assert out == json.dumps(INIT_FORCED, indent=2) + "\n"


def test_the_document_does_not_depend_on_who_is_watching(monkeypatch: pytest.MonkeyPatch) -> None:
    _, piped = _run(monkeypatch, ["init", "--json"], INIT_FORCED, tty=False)
    _, at_a_terminal = _run(monkeypatch, ["init", "--json"], INIT_FORCED, tty=True)
    assert at_a_terminal == piped == json.dumps(INIT_FORCED, indent=2) + "\n"


def test_the_json_flag_is_accepted_before_the_subcommand_too(monkeypatch: pytest.MonkeyPatch) -> None:
    _, after = _run(monkeypatch, ["init", "--json"], INIT_FORCED, tty=False)
    _, before = _run(monkeypatch, ["--json", "init"], INIT_FORCED, tty=False)
    assert before == after


def test_a_pipe_is_rendered_like_anything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """The inversion, stated once: not a terminal is no longer an answer.

    Every wrapper that shows a person what this command said had to capture it
    first, and capturing was read as a machine. The caller that really is a
    machine is the one that now says so.
    """
    code, out = _run(monkeypatch, ["init"], INIT_FORCED, tty=False)
    assert code == 0
    assert not out.lstrip().startswith("{"), out
    assert "Restart Claude Code" in out, out


def test_every_subcommand_accepts_the_json_flag_and_documents_it() -> None:
    parser = cli.build_parser()
    assert "--json" in parser.format_help()
    subparsers = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    assert subparsers.choices
    for name, subparser in subparsers.choices.items():
        options = {option for action in subparser._actions for option in action.option_strings}
        assert "--json" in options, f"{name} does not accept --json"
        assert JSON_FLAG_HELP.split(".")[0] in " ".join(subparser.format_help().split()), name


def test_the_exit_code_does_not_depend_on_who_is_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    document, _ = _run(monkeypatch, ["init", "--json"], INIT_FAILED, tty=False)
    rendered, _ = _run(monkeypatch, ["init"], INIT_FAILED, tty=False)
    assert document == rendered == 1


def test_the_protocol_streams_are_never_rendered() -> None:
    assert set(PROTOCOL_COMMANDS) == {"mcp-stdio", "com-stdio"}
    for command in PROTOCOL_COMMANDS:
        assert cli.human_readable_output(command, json_requested=False) is False


def test_a_refusal_asked_for_as_a_document_is_still_the_document_it_was(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(args: argparse.Namespace) -> None:
        raise cli.ConfigError("config_file_not_found", "No authoritative Agentic HIL configuration was found.", {"field": "workspace_root"})

    monkeypatch.setattr(cli, "dispatch", refuse)
    stream = FakeStdout(tty=False)
    monkeypatch.setattr("sys.stdout", stream)
    assert cli.entrypoint(["doctor", "--json"]) == 1
    document = json.loads(stream.getvalue())
    assert document["ok"] is False
    assert document["error_type"] == "config_file_not_found"
    assert document["remediation"] == remediation_fields("config_file_not_found", "workspace_root")["remediation"]


def test_a_refusals_nested_diagnostics_reach_the_document_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rendering gained a body; the contract did not move.

    `install.stderr` is what the human half was missing and what every machine
    caller has always parsed, so the flagged half is pinned here byte for byte,
    newlines and all, against a change to the other half.
    """
    code, out = _run(monkeypatch, ["upgrade", "--json"], UPGRADE_FAILED, tty=False)
    assert code == 1
    assert out == json.dumps(UPGRADE_FAILED, indent=2) + "\n"
    assert json.loads(out)["install"] == UPGRADE_FAILED["install"]


def test_the_decision_is_declared_and_never_sniffed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`isatty` is not consulted, so a stream that cannot answer is not a case.

    The old rule asked the stream, which meant a closed stdout, a stream without
    the method, and a captured pipe all landed on the machine document. Two of
    those were accidents of the question rather than answers to it.
    """
    for tty in (True, False):
        _, out = _run(monkeypatch, ["init"], INIT_FORCED, tty=tty)
        assert not out.lstrip().startswith("{"), tty
    assert cli.human_readable_output("init", json_requested=False) is True
    assert cli.human_readable_output("init", json_requested=True) is False


def test_a_stream_that_cannot_say_whether_it_is_a_terminal_is_rendered_anyway(monkeypatch: pytest.MonkeyPatch) -> None:
    class Bare:
        def __init__(self) -> None:
            self.text = ""

        def write(self, text: str) -> int:
            self.text += text
            return len(text)

        def flush(self) -> None:
            return None

    stream = Bare()
    monkeypatch.setattr(cli, "dispatch", lambda args: INIT_FORCED)
    monkeypatch.setattr("sys.stdout", stream)
    assert cli.entrypoint(["init"]) == 0
    assert not stream.text.lstrip().startswith("{"), stream.text


# ---------------------------------------------------------------------------
# The rendering, whose standard is that nothing actionable is lost.


def _rendered(document: object, command: str) -> str:
    return render_result(document, command)


def _reflowed(text: str) -> str:
    """The rendering with the line breaks the wrapper put in taken back out.

    What a rendering says and where it says it are two assertions, and only the
    second one is about the terminal. A path never wraps, so counting one in the
    rendered text counts copies at any width; a command is several words and
    wraps exactly like the prose around it, so counting one there counts copies
    at one width and splits it at the next.
    """
    return " ".join(text.split())


def test_the_forced_init_a_person_runs_reads_as_prose() -> None:
    out = _rendered(INIT_FORCED, "init")
    assert out.startswith("Agentic HIL project configured.")
    assert '"ok"' not in out
    assert not out.lstrip().startswith("{")
    for step in ("config", "doctor", "agent write restriction"):
        assert step in out
    assert "Next step" in out
    assert "Restart Claude Code so it loads the MCP server." in out
    assert "tightened /home/op/.local/bin" in out
    assert "This debugger names no probe_id." in out
    assert "/home/op/.config/agentic-hil/projects/blinky/config.yaml" in out


def test_a_step_that_was_not_reached_is_not_called_a_failure() -> None:
    out = _rendered(INIT_FAILED, "init")
    assert "not reached" in out
    assert "FAILED" in out
    # The one step that actually broke names its error type, so the reader stops
    # at it instead of at the step after it.
    assert "config_stale" in out
    assert "put back the way it found it" in out


def test_a_refusal_renders_its_type_its_summary_and_numbered_remediation() -> None:
    out = _rendered(REFUSAL, "doctor")
    assert out.startswith("Refused: config_file_not_found")
    assert "No authoritative Agentic HIL configuration was found for this workspace." in out
    assert "What to do" in out
    assert "1. Run `agentic-hil setup --agent <agent>` from this project root." in out
    assert "2. Or point AGENTIC_HIL_CONFIG at its absolute path." in out
    assert "Do not" in out
    assert "Do not write the configuration into the repository." in out


def test_a_refusal_that_carries_no_remediation_still_gets_the_catalogue_one() -> None:
    bare = {"ok": False, "error_type": "config_file_not_found", "summary": "It is not there."}
    out = _rendered(bare, "doctor")
    first = remediation_fields("config_file_not_found")["remediation"][0]
    assert "What to do" in out
    assert first.split(";")[0].split(",")[0] in " ".join(out.split())


def _missing_configuration_refusal() -> dict:
    """The refusal `agentic-hil doctor` produces before anything is configured."""
    return {
        "ok": False,
        "error_type": "config_file_not_found",
        "summary": "Agentic HIL configuration file could not be found.",
        **remediation_fields("config_file_not_found"),
    }


def test_the_missing_configuration_refusal_leads_with_the_route_its_reader_has() -> None:
    """#416: a person at a shell was handed the agent's job description.

    `agentic-hil doctor` before any configuration exists is where a stranger
    meets this refusal, and it opened with "Over MCP, call
    `project_config_create` once" and went on to "report what it granted ... and
    ask the operator", which is what the agent is told to do, read out to the
    operator. The command that person could actually run stood second and spoke
    about them in the third person.

    Nothing is withheld from either reader: the two orderings are made of the
    same steps, so the other route moves down the list rather than out of it."""
    at_a_shell = _reflowed(_rendered(_missing_configuration_refusal(), "doctor"))
    as_a_document = _reflowed(render_result(_missing_configuration_refusal()))

    assert "1. At a shell, run `agentic-hil init` from the project root" in at_a_shell
    assert "2. Over MCP, call `project_config_create` once." in at_a_shell
    assert "1. Over MCP, call `project_config_create` once." in as_a_document
    assert "2. At a shell, run `agentic-hil init` from the project root" in as_a_document


def test_the_shell_route_names_the_command_that_registers_the_agent_too() -> None:
    """`init` is the project half; the other half is why `setup` exists.

    A person whose agent on this machine is theirs to register needs the command
    that does both, and being sent to `init` alone leaves the skill uninstalled
    and no MCP server registered, which is the state this refusal is about."""
    at_a_shell = _reflowed(_rendered(_missing_configuration_refusal(), "doctor"))

    assert "`agentic-hil setup --agent <claude-code|codex|opencode>`" in at_a_shell


def test_a_refusal_carrying_a_list_of_its_own_is_printed_in_the_order_it_carries() -> None:
    """The reordering is the catalogue's, and only over the catalogue's own steps.

    `REFUSAL` names the same `error_type` and carries two steps a caller wrote,
    so there is nothing here for that entry's ordering to be an ordering of.
    Applying it anyway would drop both of them and print four the caller never
    sent."""
    out = _reflowed(_rendered(REFUSAL, "doctor"))

    assert "1. Run `agentic-hil setup --agent <agent>` from this project root." in out
    assert "2. Or point AGENTIC_HIL_CONFIG at its absolute path." in out
    assert "project_config_create" not in out


def test_a_permission_refusal_prints_the_key_and_the_line_that_opens_it() -> None:
    """#443: a refusal an operator can act on without opening the file.

    The rendering is where the operator meets this, and it showed a sentence
    about "the authoritative config" with no key in it and no command under it.
    The key is in the summary, in the Details rows and inside the advice, and
    the advice is the one that opens that key rather than the one that rewrites
    the whole file.
    """
    key = "debuggers.dut.permissions.allow_reset"
    refusal = {
        "ok": False,
        "tool": "reset_target",
        "error_type": "permission_denied",
        "summary": f"Target reset is disabled by the authoritative config. The permission is `{key}` and it is false.",
        "permission": key,
    }

    out = _reflowed(_rendered(refusal, "test-reactor"))

    assert "Refused: permission_denied" in out
    assert f"permission {key}" in out
    assert f"agentic-hil grant {key}" in out
    # The generic shape must never reach a reader who has an actual key.
    assert "<section>.<name>.permissions.<key>" not in out
    # And the whole-file reset is named only as the thing this is not.
    assert "agentic-hil grant" in out


def test_a_permission_refusal_that_names_no_key_grows_no_advice_about_one() -> None:
    """The other half of #443's rule: no key, no keyed advice.

    `permission_denied` is one error_type over two unlike refusals. A dump over
    `debug.max_dump_size_bytes` carries it and is not about a permission key at
    all, and handing that reader "the operator opens exactly that key" with a
    placeholder in it would be advice they cannot follow about a key that does
    not exist.
    """
    out = _reflowed(_rendered(
        {"ok": False, "tool": "debug_dump_symbol_ihex", "error_type": "permission_denied", "summary": "Symbol dump exceeds debug.max_dump_size_bytes."},
        "test-reactor",
    ))

    assert "Refused: permission_denied" in out
    assert "What to do" not in out
    assert "<section>.<name>.permissions.<key>" not in out


def test_a_refusal_the_catalogue_does_not_cover_invents_no_advice() -> None:
    bare = {"ok": False, "error_type": "no_such_error_anybody_wrote", "summary": "Something went wrong."}
    out = _rendered(bare, "doctor")
    assert out.startswith("Refused: no_such_error_anybody_wrote")
    assert "What to do" not in out
    assert "Something went wrong." in out


def test_a_refusal_shows_the_failing_tools_own_words_and_not_only_the_facts_around_them() -> None:
    """Issue #314: the one field that says why was the one field that was dropped.

    Everything else in this document rendered. The operator was told that uv
    failed, which command ran, which interpreter answered and that the old
    version was still there, and had to run the command again with `--json` to
    read the error their own machine had already produced.
    """
    out = _rendered(UPGRADE_FAILED, "upgrade")
    assert out.startswith("Refused: upgrade_failed")
    # The facts that already rendered still render, in the same section.
    for fact in ("manager", "uv", "previous_version", "0.16.0", "installation_intact"):
        assert fact in out, fact
    # And the diagnosis is under the key that carries it, with the returncode
    # beside it rather than somewhere else.
    assert "\n  install\n" in out
    assert "\n    returncode  2\n" in out
    assert "\n    stderr\n" in out
    # Captured output is literal: the manager's lines arrive as the manager
    # wrote them, indented and neither reflowed nor merged into one strip. This
    # is width-independent by construction, which the wrapped prose is not.
    assert "\n      error: Failed to install agentic-hil\n" in out
    assert "\n        Caused by: Failed to fetch `https://pypi.org/simple/agentic-hil/`\n" in out
    assert "\n        Caused by: Request failed after 3 retries\n" in out
    # The second `_process_result` on the same refusal is rendered by the same
    # rule, so the fix is about the shape rather than about one field name.
    assert "\n  verification\n" in out
    assert "\n      0.16.0\n" in out
    # It is still a report, not a dump.
    assert "{" not in out and '"stderr"' not in out


def test_a_refusal_renders_captured_output_however_deep_the_document_nests_it() -> None:
    document = {
        "ok": False,
        "error_type": "flash_failed",
        "summary": "The flash did not complete.",
        "backend": {
            "adapter": "openocd",
            "attempt": {
                "returncode": 1,
                "stderr": "Error: init mode failed (unable to connect to the target)\nin procedure 'program'",
                "probe": {"serial_number": "066EFF3"},
            },
        },
    }
    out = _rendered(document, "flash")
    # Each level under its parent, one indent further in, all the way down.
    assert "\n  backend\n" in out
    assert "\n    adapter  openocd\n" in out
    assert "\n    attempt\n" in out
    assert "\n      returncode  1\n" in out
    assert "\n      stderr\n" in out
    assert "\n        Error: init mode failed (unable to connect to the target)\n" in out
    assert "\n        in procedure 'program'\n" in out
    # The object below the captured stream is not lost behind it.
    assert "\n      probe\n" in out
    assert "066EFF3" in out


def test_a_refusal_whose_diagnosis_is_a_list_of_objects_renders_the_entries() -> None:
    """The other half of the shape, and the catalogue already points at it.

    `upgrade_in_open_run` says "`held_devices` and `device_holds` in this refusal
    name what is held" in its own remediation, so the advice this same rendering
    prints was sending people to fields it did not print.
    """
    document = {
        "ok": False,
        "error_type": "upgrade_in_open_run",
        "summary": "Something is holding this bench right now, so nothing was changed.",
        "device_holds": [
            {"resource": "debugger:dut", "holder_pid": 4412},
            {"resource": "com:dut_uart", "holder_pid": 7781},
        ],
    }
    out = _rendered(document, "upgrade")
    assert "\n  device_holds\n" in out
    assert "4412" in out and "7781" in out
    assert "debugger:dut" in out and "com:dut_uart" in out
    assert "{" not in out and "[" not in out


# The one string a copy of the audit failure cannot be counted without. Prose
# wraps to the terminal and a path never does, so this is what says how many
# times the finding was printed.
REDIRECTED_PARENT = "C:/Users/op/AppData/Local/Packages/host/LocalCache/Local/agentic-hil/projects/blinky/reports"


def _audit_refusal() -> dict:
    """A plan refused because its audit trail could not be written.

    The shape `report.audit_unavailable` and `test_reactor.propagate_result_status`
    build together, which is the one an operator met on the redirected profile of
    #387: the step's own refusal, the aggregate's `audit_error`, and the
    aggregate's `audit_errors` flattened from every step, all three holding the
    same object.
    """
    audit_error = {
        "ok": False,
        "error_type": "unsafe_configured_path",
        "summary": "Configured file's parent directory resolves to a different location than it names.",
        "field": "state_root",
        "path": "C:/Users/op/AppData/Local/agentic-hil/projects/blinky/reports/report-state.json",
        "resolved_parent": REDIRECTED_PARENT,
    }
    step_result = {
        "ok": False,
        "tool": "flash_firmware",
        "error_type": "audit_unavailable",
        "summary": "Hardware action was not started because audit output is unavailable.",
        "side_effect_committed": False,
        "audit_ok": False,
        "audit_error": audit_error,
    }
    return {
        "ok": False,
        "tool": "test_reactor",
        "error_type": "audit_unavailable",
        "summary": "Hardware action was not started because audit output is unavailable.",
        "failed_step": 1,
        "step_error_type": "audit_unavailable",
        "steps": [{"index": 1, "route": "dut", "action": "flash", "result": step_result}],
        "audit_ok": False,
        "audit_error": audit_error,
        "audit_errors": [audit_error],
    }


def test_an_audit_failure_is_printed_once_and_not_in_each_field_that_carries_it() -> None:
    """Three fields, one finding, and a person reads it where it happened.

    `audit_error` is `audit_errors[0]` wherever one is built, and a plan's
    top-level `audit_errors` is its steps' flattened into one list. The document
    is right to carry all three, and the rendering printed the same paragraph and
    the same five remediation items three times over, the last two copies with no
    step beside them to say which action they were about.
    """
    out = _rendered(_audit_refusal(), "test-reactor")

    assert out.count(REDIRECTED_PARENT) == 1
    # It survives where it explains something: under the step that met it.
    before, _, _after = out.partition(REDIRECTED_PARENT)
    assert "action  flash" in before
    assert "\n  audit_error\n" not in out
    assert "\n  audit_errors\n" not in out
    # The remediation comes with the one copy that is left, rather than being the
    # thing that was repeated. Counted with the line breaks taken back out, for
    # the reason REDIRECTED_PARENT is counted with them still in: this one is
    # prose, and where prose breaks is the terminal's business.
    assert _reflowed(out).count("agentic-hil init --force") == 1


def test_the_suite_pins_the_terminal_width_it_renders_at() -> None:
    """Which width a rendering test compares text at is part of its fixture.

    Nothing sets COLUMNS for a pytest process, so the module would wrap to
    whatever the runner leaves it, and on Linux that is two answers for one
    suite: its own 88 column fallback in a single process, and COLUMNS=80 in a
    pytest-xdist worker, which inherits the 80x24 default GNU readline writes
    into the C environment when the parent imports `readline` (#390).
    `isolated_config_environment` pins it the way it pins HOME and TMPDIR. Read
    back through the same call the module makes, with a fallback of nothing, so
    removing the pin fails here rather than moving prose in every other rendering
    test.
    """
    assert shutil.get_terminal_size(fallback=(0, 0)).columns == 88


@pytest.mark.parametrize("columns", [62, 78, 80, 88, 120])
def test_an_audit_refusal_says_the_same_things_at_every_terminal_width(monkeypatch: pytest.MonkeyPatch, columns: int) -> None:
    """The width decides where the lines break and nothing else.

    Pinning the width keeps the suite's own comparisons stable; it must not be
    what makes them true. COLUMNS=80 is what a pytest-xdist worker on Linux is
    given, and the 78 columns of prose that leaves is where the wrap fell between
    `agentic-hil` and `init --force`, so the one copy of the remediation that is
    printed stopped being countable while nothing about it had changed (#390).
    """
    monkeypatch.setenv("COLUMNS", str(columns))
    monkeypatch.setenv("LINES", "24")

    out = _reflowed(_rendered(_audit_refusal(), "test-reactor"))

    assert out.count(REDIRECTED_PARENT) == 1
    assert out.count("agentic-hil init --force") == 1
    # The whole remediation once, counted on a token no width can break, so the
    # two assertions above cannot agree by both being zero.
    assert out.count("agentic-hil://reference/platform-paths") == 1


def test_an_audit_failure_no_step_carries_is_still_printed() -> None:
    """The rule is "not twice", not "not at all".

    A run that failed before its first step, or a cleanup whose record could not
    be written, holds an audit failure nothing else in the document repeats. That
    one has nowhere else to be read, so it stays exactly where it is.
    """
    document = _audit_refusal()
    document.pop("steps")

    out = _rendered(document, "test-reactor")

    assert out.count(REDIRECTED_PARENT) == 1
    assert "\n  audit_errors\n" in out


def test_a_list_of_refusals_is_headed_by_what_each_refusal_says() -> None:
    """`- no` is not a name, and it was the name every refusal list gave.

    A list entry is headed by its first scalar field, and `ConfigError.to_dict`
    opens with `ok: False`, so an `audit_errors` list opened each entry with the
    rendering of that boolean and then folded five numbered remediation items
    into one comma-joined row. A result-shaped entry is a result wherever it
    stands, so it is rendered as one.
    """
    document = {
        "ok": False,
        "tool": "bench_run_stop",
        "error_type": "audit_unavailable",
        "summary": "The run was closed and its audit trail was not written.",
        "audit_ok": False,
        "audit_errors": [
            {"ok": False, "error_type": "unsafe_configured_path", "summary": "The reports directory resolves elsewhere.", "field": "state_root"},
            {"error_type": "OSError", "backend_error": "no space left on device"},
        ],
    }

    out = _rendered(document, "bench-run-stop")

    assert "- no" not in out
    assert "\n    - The reports directory resolves elsewhere.\n" in out
    # The numbered remediation the catalogue holds for it, as a refusal prints it
    # anywhere else, rather than one comma-joined strip.
    assert "\n          1. Read `resolved_parent` first" in out
    # An entry that is not result-shaped keeps the older head, which is the field
    # that names it rather than the first one that happens to be a boolean.
    assert "\n    - OSError\n" in out


def test_captured_output_that_is_enormous_says_how_much_it_cut_and_keeps_both_ends() -> None:
    """A truncation nobody can see is the same defect one level down.

    The line that names the cause of a failed run sits at either end of the
    capture depending on what ran, so the cut is taken out of the middle, and it
    is stated: a report that silently kept the first screenful would have been
    the same kind of quiet as the one this replaced.
    """
    document = {
        "ok": False,
        "error_type": "upgrade_failed",
        "summary": "It failed.",
        "install": {"returncode": 1, "stdout": "\n".join(f"line {number}" for number in range(1, 121)), "stderr": "y" * 900},
    }
    out = _rendered(document, "upgrade")
    assert "\n      line 1\n" in out
    assert "\n      line 120\n" in out
    assert "\n      line 60\n" not in out
    assert "[... 85 lines not shown ...]" in out
    # A single line nothing would ever read to the end of is cut the same way,
    # and counted in the unit it was cut by.
    assert "[... 400 characters not shown ...]" in out
    assert "y" * 500 in out


def test_a_secret_named_value_inside_a_refusals_nested_object_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rendering redacts through the same function the document path does.

    It is handed the redacted document, so this holds at every depth rather than
    at the top level only, and surfacing nested diagnostics could not open a
    path the machine document does not already have.
    """
    document = {
        "ok": False,
        "error_type": "upgrade_failed",
        "summary": "The manager failed.",
        "install": {"returncode": 1, "stderr": "error: 401 Unauthorized", "index": {"url": "https://pypi.example/simple", "api_token": "hunter2"}},
    }
    _, out = _run(monkeypatch, ["upgrade"], document, tty=True)
    assert "hunter2" not in out
    assert "[redacted]" in out
    # And the diagnosis that had to be surfaced for this to matter is there.
    assert "error: 401 Unauthorized" in out


def test_an_upgrade_that_left_servers_running_prints_which_hosts_to_restart() -> None:
    """The list the old refusal printed, moved to where it is no longer a wall.

    An operator who typed `agentic-hil upgrade` while their agent host was open
    used to be refused and handed pids to go and kill. They now get the upgrade
    and the same pids, under Restart, as the one thing still to do. The sentence
    comes first because it is what makes the list mean something, and the
    processes are rendered the way every other list of named things is.
    """
    document = {
        "ok": True,
        "tool": "agentic_hil_upgrade",
        "summary": "Agentic HIL upgraded from 0.19.0 to 9.9.9; restart agent hosts to load the new MCP server.",
        "previous_version": "0.19.0",
        "version": "9.9.9",
        "manager": "uv",
        "upgraded_on_disk": True,
        "restart_required": True,
        "restart_required_by": [
            {"pid": 732, "image": "C:/Users/op/AppData/Roaming/uv/tools/agentic-hil/Scripts/python.exe"},
            {"pid": 5140, "image": "C:/Users/op/AppData/Roaming/uv/tools/agentic-hil/Scripts/python.exe"},
        ],
        "restart_required_by_count": 2,
        "restart_notice": (
            "2 processes started out of this installation before the swap and still run 0.19.0; `restart_required_by` "
            "names each one by pid and image. Each goes on answering with 0.19.0 until the host that started it "
            "restarts, and that restart is the whole of what is left to do."
        ),
        "superseded_entrypoints": ["C:/Users/op/.local/bin/agentic-hil.exe.superseded"],
    }
    out = _rendered(document, "upgrade")

    assert "\nRestart\n" in out
    assert "until the host that started it restarts" in _reflowed(out)
    assert "\n  - 732\n" in out and "\n  - 5140\n" in out
    assert out.count("image  C:/Users/op/AppData/Roaming/uv/tools/agentic-hil/Scripts/python.exe") == 2
    # The renamed launcher is named rather than left as a mystery file beside the
    # one on PATH, and it is stated as nothing to act on.
    assert "\nSuperseded launchers\n" in out
    assert "agentic-hil.exe.superseded" in out
    assert "nothing needs doing about them here" in _reflowed(out)
    assert "{" not in out and "[" not in out


def test_doctor_renders_its_findings_section_by_section() -> None:
    out = _rendered(DOCTOR, "doctor")
    for section in ("Installation", "MCP registration", "Target", "Debuggers", "COM ports", "CAN buses", "Warnings"):
        assert section in out, section
    assert '"ok"' not in out
    # The per-debugger findings, each in its own words.
    assert "dut (openocd, bound)" in out
    assert "probe_id" in out and "not set" in out
    assert "debugger_not_found" in out
    assert "undetermined" in out
    assert "interface/stlink.cfg" in out and "search_name" in out
    assert "granted: allow_flash" in out and "closed: allow_mass_erase" in out
    assert "/dev/ttyACM0" in out
    assert "None configured." in out
    assert "com_ports.dut_uart is identified by a device name the host can reassign." in out
    assert "Compared with a running server" in out


def test_doctor_names_the_skipped_debugger_check_nothing_else_would_say() -> None:
    skipped = {
        **DOCTOR,
        "debuggers": {},
        "debugger": {"ok": True, "tool": "debugger_info", "skipped": True, "summary": "Debugger check skipped: set `debuggers.<name>.executable` to your OpenOCD binary."},
    }
    out = _rendered(skipped, "doctor")
    assert "Debugger check" in out
    # Wrap-insensitive, like the other renderer assertions in this module: a
    # worker process under `pytest -n auto` has no parent terminal, and the
    # renderer wraps this sentence at whatever width it falls back to, which
    # reproducibly split it between "OpenOCD" and "binary" under xdist while
    # every serial run kept it on one line.
    assert "set `debuggers.<name>.executable` to your OpenOCD binary." in " ".join(out.split())


def test_the_restart_notice_is_printed_once_however_the_document_carries_it() -> None:
    notice = "Restart Claude Code before the agentic-hil MCP tools appear."
    document = {
        "ok": True,
        "tool": "agentic_hil_agent_install",
        "scope": "user",
        # `_with_restart_notice` puts the sentence in both places on purpose, so
        # a caller reading only `summary` still gets it.
        "summary": f"Agentic HIL agent integration installed for this user account. {notice}",
        "agent": "claude-code",
        "restart_required": True,
        "restart_notice": notice,
        "permission_changes": [],
        "rollback": {"attempted": False, "ok": True, "errors": []},
        "steps": {"skill_install": {"ok": True, "summary": "Skill installed."}, "mcp_config": {"ok": True, "summary": "Registered."}},
    }
    out = render_result(document, "agent-install")
    # Reflowed, because the rendering wraps to the terminal and the sentence in
    # the summary is broken across lines.
    assert " ".join(out.split()).count(notice) == 1
    assert "\nRestart\n" not in out

    separate = {**document, "summary": "Agentic HIL agent integration installed for this user account."}
    out = render_result(separate, "agent-install")
    assert " ".join(out.split()).count(notice) == 1
    assert "\nRestart\n" in out


def test_setup_names_both_halves_the_way_troubleshooting_says_it_does() -> None:
    document = {
        "ok": False,
        "tool": "agentic_hil_setup",
        "summary": "Agentic HIL is installed for this agent under this user account; the project half failed.",
        "agent": "claude-code",
        "state_root_changes": [],
        "permission_changes": [],
        "rollback": {"attempted": True, "ok": True, "errors": []},
        "scopes": {
            "user": {"ok": True, "scope": "user", "summary": "Installed for this user account.", "command": "/home/op/.local/bin/agentic-hil"},
            "project": {"ok": False, "scope": "project", "summary": "The project half was refused.", "config_path": "/home/op/.config/agentic-hil/projects/blinky/config.yaml"},
        },
        "steps": {
            "config": {"ok": False, "error_type": "unsafe_configured_path", "summary": "A component of the path is not what the path claims."},
            "skill_install": {"ok": True, "summary": "Skill installed."},
            "mcp_config": {"ok": True, "summary": "Registered."},
            "doctor": {"ok": False, "skipped": True, "summary": "Doctor was not reached."},
            "agent_write_restriction": {"ok": False, "skipped": True, "summary": "Agent write restriction was not reached."},
        },
        "next_step": "Fix what its `config` step reports, then run `agentic-hil init --agent claude-code`.",
    }
    out = render_result(document, "setup")
    assert "Halves" in out
    assert "user (agent-install)" in out and "project (init)" in out
    assert "unsafe_configured_path" in out
    # The one refusal in the run brings its remediation with it, where the
    # failing step is, rather than being left to a second command.
    assert "Do not delete or overwrite" in " ".join(out.split())
    assert "Next step" in out


def test_a_command_with_no_renderer_of_its_own_is_still_readable() -> None:
    out = _rendered(GENERIC, "lease-status")
    assert out.startswith("Nothing on this bench is held and nothing is standing.")
    assert '"ok"' not in out
    assert "{" not in out
    assert "project_resource" in out and "project:blinky" in out
    assert "owner_active" in out and "no" in out
    assert "Next step" in out and "Nothing to do." in out


def test_the_generic_renderer_unfolds_a_list_of_objects_rather_than_dumping_it() -> None:
    document = {
        "ok": True,
        "summary": "Two sections would be left for a restart.",
        "not_reloaded_sections": [
            {"section": "permissions", "why": "the project grants. Never re-read."},
            {"section": "state_root", "why": "moving it would orphan the leases."},
        ],
    }
    out = _rendered(document, "config-reload")
    assert "- permissions" in out
    assert "the project grants. Never re-read." in out
    assert "- state_root" in out
    assert "{" not in out and "[" not in out


def test_a_result_with_nothing_but_ok_still_says_something() -> None:
    assert render_result({"ok": True}, "schema").strip() == "OK."


def test_a_document_that_claims_nothing_is_not_reported_as_a_success() -> None:
    # `overall_success` is what the exit code is taken from, and it is false for
    # a document with no `ok`. The prose has to say the same thing the exit code
    # does, or the two answer one question twice and differently.
    assert render_result({}, "schema") == "Failed.\n"


def test_a_result_that_says_ok_and_is_not_clean_says_so_before_anything_else() -> None:
    document = {
        "ok": True,
        "tool": "hardware_recover",
        "summary": "The resources were released.",
        "cleanup_required": True,
        "quarantined": True,
        "quarantine_id": "q-7f3a",
        "side_effect_status": "unknown",
    }
    out = render_result(document, "recover")
    assert "did not come back clean" in out
    assert "cleanup_required" in out and "quarantined" in out and "q-7f3a" in out
    # And it is above the detail, not buried under it.
    assert out.index("did not come back clean") < out.index("q-7f3a")


def test_the_rendering_always_ends_in_exactly_one_newline() -> None:
    for document, command in ((INIT_FORCED, "init"), (DOCTOR, "doctor"), (REFUSAL, "doctor"), (GENERIC, "lease-status")):
        out = render_result(document, command)
        assert out.endswith("\n")
        assert not out.endswith("\n\n")


def test_a_secret_named_value_is_redacted_before_it_is_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    document = {"ok": True, "summary": "Bridge opened.", "auth_token": "hunter2", "nested": {"api_key": "hunter2"}}
    _, out = _run(monkeypatch, ["lease-status"], document, tty=True)
    assert "hunter2" not in out
    assert "[redacted]" in out


@pytest.mark.parametrize("answer", [None, ["ok", "auth_token", "hunter2"]])
def test_a_redaction_that_answers_with_no_document_withholds_the_result(monkeypatch: pytest.MonkeyPatch, answer: object) -> None:
    """The fallback is the one case the guard exists for, so it fails closed.

    `redact_sensitive` answers a document with a document, which is why this has
    to be forced rather than provoked: the branch cannot be reached from a
    result's own contents, and it is precisely the branch that must not print
    the unredacted document. It used to, and a guard that falls back to the
    thing it guards against says the opposite of what it guards.

    An originally successful command that could not be rendered has still not
    been delivered, so the exit status is the refusal's, not the result's: a
    green exit here would tell a caller the command left it a document when what
    it left is the statement that there is none.
    """
    document = {"ok": True, "summary": "Bridge opened.", "auth_token": "hunter2", "nested": {"api_key": "hunter2"}}
    monkeypatch.setattr(cli, "redact_sensitive", lambda value: answer)

    code, out = _run(monkeypatch, ["lease-status"], document, tty=True)

    # An originally successful result still exits nonzero when it could not be rendered.
    assert code == 1
    # Not the secret, and not one field of the document that carried it.
    assert "hunter2" not in out
    assert "Bridge opened." not in out
    # What is there instead names the refusal, the command, and what to do.
    assert "redaction_unavailable" in out
    assert "lease-status" in out
    assert "What to do" in out
    # The remediation no longer sends the operator to `--json`: that sink shares
    # `redact_sensitive` and fails the same way, so it names it as no way around.
    assert "agentic-hil --version" in out


@pytest.mark.parametrize("answer", [None, ["ok", "auth_token", "hunter2"]])
def test_the_machine_sink_also_fails_closed_when_redaction_yields_no_document(monkeypatch: pytest.MonkeyPatch, answer: object) -> None:
    """`--json` is the sink the old remediation sent the operator to, so it is
    the one that most had to stop printing `null` or a bare list in place of the
    result. It shares `redact_sensitive` with the rendering, so under the exact
    condition the fallback exists for it dumps the same refusal document and
    exits nonzero rather than a value nothing vouched for.
    """
    document = {"ok": True, "summary": "Bridge opened.", "auth_token": "hunter2", "nested": {"api_key": "hunter2"}}
    monkeypatch.setattr(cli, "redact_sensitive", lambda value: answer)

    code, out = _run(monkeypatch, ["lease-status", "--json"], document, tty=False)

    assert code == 1
    # The machine sink is JSON, and it is the refusal rather than `null`/a list.
    refusal = json.loads(out)
    assert refusal["error_type"] == "redaction_unavailable"
    assert refusal["command"] == "lease-status"
    # Neither the secret nor the field that carried it reaches the document.
    assert "hunter2" not in out
    assert "Bridge opened." not in out


def test_a_stream_that_cannot_spell_the_text_still_gets_the_answer() -> None:
    class Narrow(io.StringIO):
        encoding = "ascii"

        def write(self, text: str) -> int:
            text.encode("ascii")
            return super().write(text)

    # The punctuation this project's own knowledge catalogue carries, which an
    # ascii console cannot spell. `json.dumps` escapes it, so the machine half
    # never met this; prose does.
    dash = "\u2014"
    stream = Narrow()
    write_rendered(stream, f"a restart adopts everything else {dash} every permission included\n")
    written = stream.getvalue()
    assert "a restart adopts everything else" in written
    assert "every permission included" in written
    assert dash not in written


# ---------------------------------------------------------------------------
# The whole command, end to end, over a configuration this test writes.


def test_a_real_doctor_at_a_terminal_reads_as_a_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "firmware"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    assert cli.entrypoint(["init", "--json"]) == 0

    stream = FakeStdout(tty=True)
    monkeypatch.setattr("sys.stdout", stream)
    # Nothing is attached on this host, so this bench is unbound and the exit
    # code says so (#433). The report is what this test is about, and it is a
    # report either way.
    assert cli.entrypoint(["doctor"]) == 1
    out = stream.getvalue()
    assert '"ok"' not in out
    assert "Installation" in out and "MCP registration" in out and "Debuggers" in out
    assert str(cli.initialized_config_path(workspace)) in out
    # The one section a newcomer needed and did not have: the bench, the reason,
    # and the command that binds it, on screen and above the device sections.
    assert "Bench binding" in out
    assert "UNBOUND" in out
    assert "adopt-hardware" in out
    assert out.index("Bench binding") < out.index("Debuggers")


def test_a_real_doctor_asked_for_as_a_document_is_one_json_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "firmware"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    assert cli.entrypoint(["init", "--json"]) == 0

    stream = FakeStdout(tty=False)
    monkeypatch.setattr("sys.stdout", stream)
    assert cli.entrypoint(["doctor", "--json"]) == 1
    document = json.loads(stream.getvalue())
    assert document["tool"] == "agentic_hil_doctor"
    # The fields a script reads for the same fact the report puts on screen.
    assert document["ok"] is False
    assert document["unhealthy"] == ["bench_binding"]
    assert document["bench_binding"]["ok"] is False
    assert "adopt-hardware" in document["bench_binding"]["next_step"]
    assert document["config_path"] == str(cli.initialized_config_path(workspace))


def test_the_rendering_never_decides_anything_the_document_did_not(monkeypatch: pytest.MonkeyPatch) -> None:
    """The renderer is a formatter. It is handed the document and returns text."""
    seen: list[object] = []

    def record(result: object, command: str | None = None) -> str:
        seen.append((result, command))
        return ""

    monkeypatch.setattr(cli, "render_result", record)
    _run(monkeypatch, ["init"], INIT_FORCED, tty=True)
    assert seen == [(INIT_FORCED, "init")]


def test_a_probe_listing_says_which_enumeration_produced_the_ids() -> None:
    """On an OpenOCD bench the backend and the enumeration are two facts (#432).

    The ids come from the USB descriptors the probes published to this host, not
    from anything OpenOCD ran, and a reader deciding whether a serial was read
    off the probe or off its descriptor cannot get that from `backend: openocd`
    alone. The document carries `discovered_by`; the rendering has to show it.
    """
    listing = {
        "ok": True,
        "tool": "debugger_probes_list",
        "backend": "openocd",
        "discovered_by": "usb_serial_inventory",
        "probes": [{"probe_id": "066AFF303435554157113106"}],
        "summary": "1 connected debugger probe(s) read from this host's USB serial inventory.",
    }

    out = _rendered(listing, "debugger-probes")

    assert "discovered_by" in out
    assert "usb_serial_inventory" in out
    assert "066AFF303435554157113106" in out


def test_a_probe_listing_refused_on_an_adapter_nothing_enumerates_says_which_script() -> None:
    """The surviving `not_supported`, with the fact that decided it on screen."""
    refusal = {
        "ok": False,
        "tool": "debugger_probes_list",
        "backend": "openocd",
        "error_type": "not_supported",
        "interface_cfg": "interface/jlink.cfg",
        "summary": "OpenOCD has no command that enumerates connected probe IDs, and this entry's adapter is not one this host can enumerate from its USB serial inventory either: `interface_cfg` is `interface/jlink.cfg`.",
    }

    out = _reflowed(_rendered(refusal, "debugger-probes"))

    assert "not_supported" in out
    assert "interface/jlink.cfg" in out


# ---------------------------------------------------------------------------
# The adoption plan a person reads before they let it write (#442).


ADOPT_DRY_RUN = {
    "ok": True,
    "tool": "project_config_adopt_hardware",
    "summary": "3 configuration key(s) would be filled in from the attached hardware.",
    "applied": False,
    "path": "/home/op/.config/agentic-hil/projects/blinky/config.yaml",
    "debugger_id": "dut",
    "com_port_id": "dut_uart",
    "carried": [{"key": "debuggers.dut.probe_id", "value": "066AFF303435554157113106", "previous_value": None}],
    "already_current": [{"key": "debug.gdb_executable", "value": "/usr/bin/arm-none-eabi-gdb"}],
    "kept": [
        {
            "key": "target.controller",
            "configured_value": "stm32f446ret6",
            "discovered_value": "stm32f4x",
            "reason": (
                "`target.controller` names the part this project drives, and what a read of the board reports is the "
                "target script that answered, which is a family rather than a part."
            ),
        }
    ],
}


def test_the_plan_shows_the_two_values_it_compared_rather_than_two_blanks() -> None:
    """Both values, the way the same plan's JSON carries them (#442).

    The rendering read `configured` and `discovered`, which nothing writes: the
    producer's names are `configured_value` and `discovered_value`. Every
    comparison an operator came here to see therefore printed "configured not
    set, attached not set" beside a JSON document holding both, which says a key
    is empty while it holds the value that decided the comparison.
    """
    out = _reflowed(_rendered(ADOPT_DRY_RUN, "adopt-hardware"))

    assert "target.controller configured stm32f446ret6, attached stm32f4x" in out
    assert "not set" not in out


def test_the_plan_says_which_of_the_two_values_adoption_keeps_and_why() -> None:
    """Naming the survivor is the half an operator has to act on.

    "Left alone" says a decision was taken. Which value it left standing, and on
    what grounds, is what decides whether the operator edits the file or leaves
    it, and both were dropped from the rendering while the JSON carried them.
    """
    out = _reflowed(_rendered(ADOPT_DRY_RUN, "adopt-hardware"))

    assert "adoption keeps stm32f446ret6" in out
    assert "family rather than a part" in out


def test_the_plan_still_renders_the_boxes_the_comparison_is_not_in() -> None:
    """The neighbouring sections are untouched by the row above them."""
    out = _reflowed(_rendered(ADOPT_DRY_RUN, "adopt-hardware"))

    assert "Would be filled in" in out
    assert "debuggers.dut.probe_id 066AFF303435554157113106" in out
    assert "Already match the attached hardware" in out
    assert "debug.gdb_executable" in out


def test_a_plan_written_under_the_older_field_names_still_shows_its_values() -> None:
    """The two spellings the rendering used to read are still read.

    They are not what this repository writes, and a document that carries them
    must not be the one case that renders the blanks this defect was about.
    """
    older = {
        **ADOPT_DRY_RUN,
        "kept": [{"key": "target.controller", "configured": "stm32f446ret6", "discovered": "stm32f4x", "reason": "Somebody set it."}],
    }

    out = _reflowed(_rendered(older, "adopt-hardware"))

    assert "target.controller configured stm32f446ret6, attached stm32f4x" in out
    assert "not set" not in out


def test_a_healthy_probe_listing_is_not_rendered_as_a_containment() -> None:
    """`complete: false` is a fact about the enumeration, not about this run (#445).

    An OpenOCD bench enumerates probes from the host's USB serial inventory,
    which can never report itself complete, so the field is false on every
    healthy read. The rendering put it under "This did not come back clean, and
    the exit code says so", beside an exit code that has stopped saying it: a
    discovery that worked was shown to an operator as a fault.
    """
    listing = {
        "ok": True,
        "tool": "debugger_probes_list",
        "backend": "openocd",
        "discovered_by": "usb_serial_inventory",
        "probes": [{"probe_id": "066AFF303435554157113106"}],
        "complete": False,
        "summary": "1 connected debugger probe(s) read from this host's USB serial inventory, which is not an authoritative count.",
    }

    out = _reflowed(_rendered(listing, "debugger-probes"))

    assert "did not come back clean" not in out
    # And it is still on screen, because how far the enumeration reaches is what
    # a reader counting probes has to know.
    assert "complete no" in out
    assert "not an authoritative count" in out


def test_a_probe_listing_with_something_standing_still_says_it_did_not_come_back_clean() -> None:
    """The neighbouring rendering, unchanged: a marker still leads the answer.

    And `complete` is not one of the things standing. It decides no verdict, so
    naming it under "what is standing" would offer it as a reason for an exit
    code it has nothing to do with; it belongs with the other facts the listing
    states about itself, which are printed below that block.
    """
    listing = {
        "ok": True,
        "tool": "debugger_probes_list",
        "backend": "openocd",
        "probes": [],
        "complete": False,
        "quarantined": True,
        "quarantine_id": "q-7f3a",
        "summary": "Probe discovery ran.",
    }

    out = _reflowed(_rendered(listing, "debugger-probes"))

    assert "did not come back clean" in out
    assert "q-7f3a" in out
    assert out.index("complete no") > out.index("backend openocd")
