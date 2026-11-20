"""`agentic-hil remote`, the thin client of a shared bench (#891).

The client runs where no Agentic HIL configuration exists: every test here
takes the configuration away from the process and makes loading one fail.
Against the real job service only the worker is faked; refusals and transport
failures are answered by the scripted service or by a port nothing listens on.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
from pathlib import Path
from typing import Any

import pytest
from bench_job_fakes import (
    IMAGE,
    IMAGE_PATH,
    TERMINAL,
    FakeWorkerSource,
    job_bench,
    open_service,
    plan,
    serving,
    stored_jobs,
    wait_for_state,
)
from httpserver_fakes import ScriptedJobService, running_http, service_error

from agentic_hil import cli
from agentic_hil.httpapi import JobStatus
from agentic_hil.types import JsonObject

SCRIPTED_JOB = "job-0123456789abcdef"
SCRIPTED_SECOND_JOB = "job-fedcba9876543210"


def accepted(job_id: str) -> JsonObject:
    return {"job_id": job_id, "job_token": f"scripted-token-{job_id}", "state": "queued", "missing_artifacts": [], "queue_position": 1, "waiting_for": []}


def refuse_configuration(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("the remote client loaded an Agentic HIL configuration")


def client_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, name: str = "client", plan_bytes: bytes, image: bool = True) -> Path:
    """A developer machine's project: a plan, maybe its image, and no Agentic HIL configuration."""
    client = tmp_path / name
    (client / ".agentic-hil").mkdir(parents=True, exist_ok=True)
    (client / ".agentic-hil" / "testconfig.yaml").write_bytes(plan_bytes)
    if image:
        (client / "build").mkdir(exist_ok=True)
        (client / IMAGE_PATH).write_bytes(IMAGE)
    monkeypatch.chdir(client)
    monkeypatch.delenv("AGENTIC_HIL_CONFIG", raising=False)
    monkeypatch.delenv("AGENTIC_HIL_BENCH_URL", raising=False)
    monkeypatch.setattr(cli, "load_cli_authoritative_config", refuse_configuration)
    return client


def remote(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, JsonObject, str]:
    """One `agentic-hil --json remote ...`: its exit code, its document and everything it printed."""
    code = cli.entrypoint(["--json", "remote", *argv])
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert isinstance(document, dict), captured.out
    return code, document, captured.out + captured.err


def token_file(client: Path, job_id: str) -> JsonObject:
    stored = json.loads((client / ".agentic-hil" / "remote" / f"{job_id}.json").read_text(encoding="utf-8"))
    assert isinstance(stored, dict)
    return stored


def test_submit_waits_for_the_run_and_exits_with_its_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    bench = job_bench(tmp_path, monkeypatch)
    workers = FakeWorkerSource()
    with open_service(bench, workers) as service, serving(service) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setattr("getpass.getuser", lambda: "ci-runner")
        for name, verdict in (("passing", 0), ("failing", 1)):
            client = client_workspace(tmp_path, monkeypatch, name=f"client-{name}", plan_bytes=plan(name=name, image=IMAGE_PATH))
            junit = client / "out" / f"{name}.xml"
            code, result, printed = remote(capsys, "submit", "--bench", url, "--wait", "--junit-xml", str(junit))
            # The exit code is the run's own verdict, as a local test-reactor gives it.
            assert code == verdict, result
            assert result["ok"] is (verdict == 0)

            (kept,) = sorted((client / ".agentic-hil" / "remote").glob("job-*.json"))
            stored = token_file(client, kept.stem)
            job_id, token = stored["job_id"], stored["job_token"]
            assert kept.name == f"{job_id}.json"
            assert stored["bench"] == url
            assert token and token not in printed
            if os.name != "nt":
                assert stat.S_IMODE(kept.stat().st_mode) == 0o600
            assert service.status(job_id).status["state"] in TERMINAL
            assert junit.read_bytes() == service.junit(job_id, job_token=token)
            # The plan and its image went in one submission, under the login name.
            assert stored_jobs(bench)[job_id]["submitter_label"] == "ci-runner"
            assert (bench.job_root / job_id / "workspace" / IMAGE_PATH).read_bytes() == IMAGE


def test_status_queue_withdraw_and_fetch_use_the_stored_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    bench = job_bench(tmp_path, monkeypatch)
    workers = FakeWorkerSource()
    with open_service(bench, workers) as service, serving(service) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        client = client_workspace(tmp_path, monkeypatch, plan_bytes=plan(name="held first"), image=False)
        code, first, _ = remote(capsys, "submit", "--bench", url)
        assert code == 0, first
        first_id = first["job_id"]
        wait_for_state(service, first_id, {"running"})
        (client / ".agentic-hil" / "testconfig.yaml").write_bytes(plan(name="held second"))
        code, second, _ = remote(capsys, "submit", "--bench", url)
        assert code == 0, second
        second_id = second["job_id"]

        code, status, _ = remote(capsys, "status", "--bench", url, "--job", second_id)
        assert code == 0, status
        assert status["state"] == "queued"
        code, queue, _ = remote(capsys, "queue", "--bench", url)
        assert code == 0, queue
        assert [entry["job_id"] for entry in queue["entries"]] == [second_id]
        code, withdrawn, _ = remote(capsys, "withdraw", "--bench", url, "--job", second_id)
        assert code == 0, withdrawn
        assert service.status(second_id).status["state"] == "withdrawn"

        # Another machine has no token for the first job, so it neither withdraws nor fetches it.
        client_workspace(tmp_path, monkeypatch, name="other-client", plan_bytes=plan(name="unused"), image=False)
        for argv in (("withdraw", "--bench", url, "--job", first_id), ("fetch", "--bench", url, "--job", first_id, "--into", "reports")):
            code, refused, _ = remote(capsys, *argv)
            assert code == 1, refused
            assert refused["error_type"] == "job_token_missing"
        assert service.status(first_id).status["state"] == "running"

        workers.finish(workers.handle_of(first_id))
        assert wait_for_state(service, first_id, TERMINAL)["state"] == "finished"
        monkeypatch.chdir(client)
        code, fetched, _ = remote(capsys, "fetch", "--bench", url, "--job", first_id, "--into", "reports")
        assert code == 0, fetched
        reports = client / "reports"
        assert json.loads((reports / "report.json").read_text(encoding="utf-8"))["ok"] is True
        assert b"<testsuite" in (reports / "junit.xml").read_bytes()
        assert (reports / f"{first_id}-evidence.zip").is_file()


def test_refusals_and_transport_errors_exit_1_and_usage_errors_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    service = ScriptedJobService()
    over_quota = service_error("max_queued_jobs_per_user", {"retry_after_s": 1})
    invalid = service_error("test_config_invalid", {"validation_error": {"error_type": "test_config_invalid", "field": "steps"}})
    service.script("submit", over_quota, invalid)
    with running_http(service) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        client = client_workspace(tmp_path, monkeypatch, plan_bytes=plan(name="refused", image=IMAGE_PATH))
        # A refusal is the server's document, passed on with its own type.
        for expected in (over_quota, invalid):
            code, result, _ = remote(capsys, "submit", "--bench", url)
            assert code == 1, result
            assert (result["error_type"], result["summary"]) == (expected.error_type, expected.summary)
        assert [call.method for call in service.calls] == ["submit", "submit"]

        # A bench nothing answers on. The port is bound and never listened on, so it stays this test's.
        silent = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        silent.bind(("127.0.0.1", 0))
        try:
            code, result, _ = remote(capsys, "status", "--bench", f"http://127.0.0.1:{silent.getsockname()[1]}", "--job", SCRIPTED_JOB)
        finally:
            silent.close()
        assert code == 1, result
        assert result["error_type"] == "bench_unreachable"

        # An image the plan names and the machine does not have is refused before any request.
        (client / IMAGE_PATH).unlink()
        code, result, _ = remote(capsys, "submit", "--bench", url)
        assert code == 1, result
        assert result["error_type"] == "remote_artifact_missing"
        assert IMAGE_PATH in json.dumps(result)
        # A job this machine holds no token for is refused before any request too.
        code, result, _ = remote(capsys, "withdraw", "--bench", url, "--job", SCRIPTED_JOB)
        assert code == 1, result
        assert result["error_type"] == "job_token_missing"
        assert len(service.calls) == 2

        # The bench may come from the environment instead of --bench.
        service.script("status", JobStatus(1, {"job_id": SCRIPTED_JOB, "state": "queued"}))
        monkeypatch.setenv("AGENTIC_HIL_BENCH_URL", url)
        code, result, _ = remote(capsys, "status", "--job", SCRIPTED_JOB)
        assert code == 0, result
        assert result["state"] == "queued"
        monkeypatch.delenv("AGENTIC_HIL_BENCH_URL")

    # A usage error is argparse's own: no bench, a scheme other than http, a JUnit file without --wait.
    for argv in (("submit",), ("submit", "--bench", "ftp://127.0.0.1:1"), ("submit", "--bench", "http://127.0.0.1:1", "--junit-xml", "out.xml")):
        with pytest.raises(SystemExit) as usage:
            cli.entrypoint(["--json", "remote", *argv])
        assert usage.value.code == 2, argv
    capsys.readouterr()


def remote_submit_help(parser: argparse.ArgumentParser) -> str:
    """`agentic-hil remote submit --help`, or nothing where the program has no such command."""
    commands = next((action for action in parser._actions if isinstance(action, argparse._SubParsersAction)), None)
    remote_parser = commands.choices.get("remote") if commands is not None else None
    if remote_parser is None:
        return ""
    actions = next((action for action in remote_parser._actions if isinstance(action, argparse._SubParsersAction)), None)
    submit = actions.choices.get("submit") if actions is not None else None
    return submit.format_help() if submit is not None else ""


def test_submit_help_says_the_login_name_is_sent_and_seen() -> None:
    help_text = " ".join(remote_submit_help(cli.build_parser()).split()).lower()
    if not all(words in help_text for words in ("--user", "login name", "self-declared", "everyone", "queue", "report")):
        raise AssertionError("remote submit --help does not say that the login name is sent by default and seen by everyone who reaches the bench")


def test_from_is_the_login_name_unless_user_names_another(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    service = ScriptedJobService()
    service.script("submit", accepted(SCRIPTED_JOB), accepted(SCRIPTED_SECOND_JOB))
    plan_bytes = plan(name="named", image=IMAGE_PATH)
    with running_http(service) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        client = client_workspace(tmp_path, monkeypatch, plan_bytes=plan_bytes)
        monkeypatch.setattr("getpass.getuser", lambda: "ci-runner")
        code, result, printed = remote(capsys, "submit", "--bench", url)
        assert code == 0, result
        assert accepted(SCRIPTED_JOB)["job_token"] not in printed
        code, result, _ = remote(capsys, "submit", "--bench", url, "--user", "alice")
        assert code == 0, result
    submitted = [call for call in service.calls if call.method == "submit"]
    assert [call.kwargs["submitted_by"] for call in submitted] == ["ci-runner", "alice"]
    # One submission each: the plan and every image it names.
    assert all(call.args == (plan_bytes, {IMAGE_PATH: IMAGE}) for call in submitted), submitted
    assert token_file(client, SCRIPTED_JOB)["job_token"] == accepted(SCRIPTED_JOB)["job_token"]
