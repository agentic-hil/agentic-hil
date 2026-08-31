"""The reactor has a door on the MCP surface, and it is the same room behind it.

The test reactor is the feature that ties every other tool together, and until
now the only way to start one was a shell command. An agent holding every
hardware tool this server has therefore had to leave all of them to run a plan,
which is the one thing those tools exist to stop: a shell command is judged by
whatever the agent host makes of it, and the operator cannot see it as a
hardware action.

So these tests ask one question in several shapes: is what comes back through
`test_reactor_run`, `test_reactor_status` and `test_reactor_stop` the same thing
`agentic-hil test-reactor` produces? Several of them answer it by running both
and comparing the results outright, because "the same" is the whole claim and a
hand-written expectation would only pin what somebody remembered of it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS, FAKE_OPENOCD, write_authoritative_config

# `detached_runs` is imported for its fixture, which a detached run in this
# module needs exactly as the lifecycle tests do: the device locks are
# machine-wide, so a worker still holding the fake probe would refuse the next
# test's run rather than let it fail its own assertion.
from test_run_lifecycle import (
    bench_workspace,
    detached_runs,  # noqa: F401
    wait_for_state,
    worker_log,
)

from agentic_hil.config import ConfigError, load_authoritative_config
from agentic_hil.tools import AgenticHILToolService, UnprovisionedToolService

RESET_PLAN = "version: 4\nsteps:\n  - {device: dut, action: reset}\n"
SHORT_DELAY_PLAN = "version: 4\nsteps:\n  - {device: dut, action: delay, duration_ms: 1200}\n"
LONG_DELAY_PLAN = "version: 4\nsteps:\n  - {device: dut, action: delay, duration_ms: 600000}\n"


def bound_service(workspace: Path) -> AgenticHILToolService:
    """A server bound to the workspace's configuration, as `mcp-stdio` binds one."""
    return AgenticHILToolService(load_authoritative_config(workspace))


def call(workspace: Path, name: str, arguments: dict) -> dict:
    service = bound_service(workspace)
    try:
        return service.call(name, arguments)
    finally:
        service.close()


def _comparable(result: dict) -> dict:
    """One run's result, less the two things two runs may honestly differ in.

    The handle, because two runs are two runs; and the moment the configuration
    was last compared against the file, because a clock moves between them. The
    digests that check says the same thing about stay in."""
    trimmed = {key: value for key, value in result.items() if key != "run"}
    in_force = trimmed.get("config_in_force")
    if isinstance(in_force, dict):
        trimmed["config_in_force"] = {key: value for key, value in in_force.items() if key != "checked_at"}
    return trimmed


def test_a_plan_run_over_mcp_answers_with_the_reactor_result_and_its_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool answers with the run, not with a note that a run happened.

    The result is the reactor's own: its verdict, its step records, the handle
    it registered and the path of the report it wrote. An agent that
    started a plan needs what the plan did, and a second call to fetch it would
    be a second chance for the two to disagree."""
    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)

    result = call(workspace, "test_reactor_run", {"test_config_path": str(plan)})

    assert result["ok"] is True, result
    assert result["tool"] == "test_reactor"
    assert [(step["index"], step["route"], step["action"]) for step in result["steps"]] == [(1, "dut", "reset")]
    assert result["steps"][0]["result"]["ok"] is True
    assert result["cleanup_ok"] is True
    assert result["report_path"] == ".agentic-hil/reports/last-report.json"
    assert result["run"].startswith("run-")
    report = json.loads((workspace / ".agentic-hil" / "reports" / "last-report.json").read_text(encoding="utf-8"))
    assert report["run"] == result["run"]


def test_a_detached_plan_started_over_mcp_answers_with_a_handle_and_then_finishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detached_runs) -> None:  # noqa: F811
    """`detach: true` answers with a handle to a run that is really under way.

    The same contract the command line's `--detach` has: the answer comes as
    soon as the worker holds the devices it declared, so a handle it returned is
    a handle with the bench, and the run then finishes on its own without the
    call that started it staying open.

    Which of the two states the answer is caught in is not that contract, so it
    is not asked. A plan short enough to finish inside a test can also finish
    inside the start command's own poll on a machine busy enough, which is #364
    in the command line's spelling of this. What is asked is the handle, the
    bench behind it and the ending, and the ending is waited for."""
    workspace, plan = bench_workspace(tmp_path, monkeypatch, SHORT_DELAY_PLAN)
    config = load_authoritative_config(workspace)

    result = call(workspace, "test_reactor_run", {"test_config_path": str(plan), "detach": True})

    detached_runs.append((config, result.get("run")))
    assert result["ok"] is True, result.get("worker_output") or result
    assert result["tool"] == "test_reactor_start"
    assert result["detached"] is True
    assert result["state"] in {"running", "finished"}, (
        f"a run this call had just launched answered {result.get('state')!r}: {result.get('summary')!r}"
    )
    assert result["run"].startswith("run-")
    assert result["report_path"] == ".agentic-hil/reports/last-report.json"
    finished = wait_for_state(config, result["run"], {"finished", "stopped", "worker_gone"})
    assert finished["state"] == "finished", (
        f"the run ended in state {finished.get('state')!r} ({finished.get('summary')!r}); its worker printed:\n"
        f"{worker_log(config, result['run'])}"
    )
    assert finished["run_ok"] is True


def test_status_over_mcp_is_the_answer_the_command_line_gives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both spellings of the status question, held to the command line's own answer.

    Named, and unnamed for the listing: a run an operator started in a terminal
    and a run an agent started over MCP are one run under one handle, so an
    answer that differed by who asked would be two records of one thing."""
    from agentic_hil.cli import run_test_reactor
    from agentic_hil.runlifecycle import run_status

    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    config = load_authoritative_config(workspace)
    finished = run_test_reactor(str(plan))

    named = call(workspace, "test_reactor_status", {"run": finished["run"]})
    listed = call(workspace, "test_reactor_status", {})

    assert named == run_status(config, finished["run"])
    assert named["ok"] is True
    assert named["state"] == "finished"
    assert named["run_ok"] is True
    assert listed == run_status(config, None)
    assert [item["run"] for item in listed["runs"]] == [finished["run"]]
    assert listed["active_runs"] == []


def test_a_stop_asked_over_mcp_ends_a_waiting_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detached_runs) -> None:  # noqa: F811
    """A run waiting out a ten-minute delay ends because the tool asked it to.

    Cooperative, exactly as the command is: the request is left under the handle,
    the run reads it inside its wait, and it then closes its devices and writes
    its report. Nothing here reaches the process, so the run cannot be left half
    way through a step by whoever asked it to stop."""
    workspace, plan = bench_workspace(tmp_path, monkeypatch, LONG_DELAY_PLAN)
    config = load_authoritative_config(workspace)
    started = call(workspace, "test_reactor_run", {"test_config_path": str(plan), "detach": True})
    detached_runs.append((config, started.get("run")))
    assert started["state"] == "running", started.get("worker_output") or started

    requested = call(workspace, "test_reactor_stop", {"run": started["run"]})

    assert requested["ok"] is True
    assert requested["stop_requested"] is True
    stopped = wait_for_state(config, started["run"], {"finished", "stopped", "worker_gone"})
    assert stopped["state"] == "stopped", worker_log(config, started["run"])
    assert stopped["error_type"] == "run_stopped"
    report = json.loads((workspace / ".agentic-hil" / "reports" / "last-report.json").read_text(encoding="utf-8"))
    assert report["stopped"] is True


def test_a_plan_outside_the_workspace_is_refused_exactly_as_the_command_line_refuses_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The workspace binding is the plan loader's, and the tool inherits it whole.

    The command line raises this and prints it; the tool returns it, with the
    fields a refusal on this surface carries and not one word of the refusal
    changed. A tool that resolved a path the command refuses would be a way
    around `workspace_root` reachable from a chat message."""
    from agentic_hil.cli import run_test_reactor

    workspace, _ = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    outside = tmp_path / "outside.yaml"
    outside.write_text(RESET_PLAN, encoding="utf-8")

    refused = call(workspace, "test_reactor_run", {"test_config_path": str(outside)})

    with pytest.raises(ConfigError) as excinfo:
        run_test_reactor(str(outside))
    at_the_command_line = excinfo.value.to_dict()
    assert refused == {"tool": "test_reactor_run", "side_effect_committed": False, **at_the_command_line}
    assert refused["error_type"] == "test_config_invalid"
    assert refused["workspace_root"] == str(workspace)


def test_a_plan_a_permission_refuses_is_refused_the_same_way_on_both_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A permission the configuration withholds is the answer, whoever asked.

    The reactor decides this before its first hardware action: preflight reads
    the permission of the device each step names and refuses the whole plan by
    step, field and reason. So the two routes cannot answer differently, because they
    run the same preflight over the same configuration. Compared outright rather
    than by a remembered field list, and the run handle is the only thing that
    may differ, because two runs are two runs."""
    from agentic_hil.cli import run_test_reactor

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    probe = tmp_path / "fake_openocd.py"
    probe.write_bytes(FAKE_OPENOCD.read_bytes())
    write_authoritative_config(
        workspace,
        monkeypatch,
        debugger_executable=probe,
        permissions={**DEFAULT_TEST_PERMISSIONS, "allow_reset": False},
    )
    monkeypatch.chdir(workspace)
    plan = workspace / ".agentic-hil" / "testconfig.yaml"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(RESET_PLAN, encoding="utf-8")

    over_mcp = call(workspace, "test_reactor_run", {"test_config_path": str(plan)})

    at_the_command_line = run_test_reactor(str(plan))
    assert over_mcp["ok"] is False
    assert over_mcp["error_type"] == "test_config_invalid"
    assert over_mcp["validation_error"]["step"] == 1
    assert over_mcp["validation_error"]["action"] == "reset"
    assert "disabled" in over_mcp["validation_error"]["summary"]
    assert over_mcp["steps"] == []
    assert _comparable(over_mcp) == _comparable(at_the_command_line)
    assert over_mcp["run"] != at_the_command_line["run"]


def test_the_reactor_tools_refuse_an_unprovisioned_workspace_like_every_other_hardware_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No configuration means no bench, no permission and no state directory.

    The three answer that the way the rest of the surface does, with the standard
    `config_file_not_found` refusal naming `project_config_create`, rather than
    raising out of a plan loader that has no workspace to hold a path to."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    service = UnprovisionedToolService(workspace)
    try:
        refusals = {
            name: service.call(name, arguments)
            for name, arguments in (
                ("test_reactor_run", {}),
                ("test_reactor_status", {}),
                ("test_reactor_stop", {"run": "run-0123456789abcdef"}),
            )
        }
    finally:
        service.close()

    for name, refused in refusals.items():
        assert refused["ok"] is False, name
        assert refused["tool"] == name
        assert refused["error_type"] == "config_file_not_found", name
        assert refused["side_effect_committed"] is False, name
        assert "project_config_create" in refused["next_step"], name
