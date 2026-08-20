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
    assert cli.entrypoint(["doctor"]) == 0
    out = stream.getvalue()
    assert '"ok"' not in out
    assert "Installation" in out and "MCP registration" in out and "Debuggers" in out
    assert str(cli.initialized_config_path(workspace)) in out


def test_a_real_doctor_asked_for_as_a_document_is_one_json_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "firmware"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    assert cli.entrypoint(["init", "--json"]) == 0

    stream = FakeStdout(tty=False)
    monkeypatch.setattr("sys.stdout", stream)
    assert cli.entrypoint(["doctor", "--json"]) == 0
    document = json.loads(stream.getvalue())
    assert document["tool"] == "agentic_hil_doctor"
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
