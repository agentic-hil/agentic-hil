from __future__ import annotations

import io
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest

from evals.install import runner as install_runner
from evals.install.config import CredentialFile
from evals.install.redaction import (
    REDACTION_MARKER,
    TRUNCATION_MARKER,
    RedactingLogWriter,
    SecretRedactor,
    redact,
    redact_value,
    secret_variants,
)
from evals.install.runner import auth_values, extract_start_metadata, run_logged

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_redact_removes_exact_json_and_url_encoded_secret_forms() -> None:
    secret = 'oauth/"tök en+\nvalue'
    json_escaped = json.dumps(secret, ensure_ascii=True)[1:-1]
    slash_escaped_json = json_escaped.replace("/", r"\/")
    url_encoded = quote(secret, safe="")
    url_encoded_lower = url_encoded.replace("%C3", "%c3").replace("%B6", "%b6")
    form_encoded = quote_plus(secret, safe="")
    message = "|".join((secret, json_escaped, slash_escaped_json, url_encoded, url_encoded_lower, form_encoded))

    result = redact(message, [secret])

    assert result.count(REDACTION_MARKER) == 6
    for variant in secret_variants([secret]):
        assert variant not in result


def test_stream_redaction_matches_secret_across_every_chunk_boundary() -> None:
    secret = 'oauth/"token value'
    encoded = quote(secret, safe="")
    message = f"before:{encoded}:after"

    for split_at in range(1, len(message)):
        redactor = SecretRedactor([secret])
        result = redactor.feed(message[:split_at])
        result += redactor.feed(message[split_at:])
        result += redactor.finish()

        assert result == f"before:{REDACTION_MARKER}:after"


def test_redact_value_covers_nested_values_and_mapping_keys() -> None:
    secret = "credential/value with space"
    value = {
        f"key-{quote(secret, safe='')}": [
            secret,
            {"nested": json.dumps(secret)[1:-1]},
        ],
        "tuple": (quote_plus(secret, safe=""),),
    }

    redacted = redact_value(value, [secret])
    serialized = json.dumps(redacted)

    assert secret not in serialized
    for variant in secret_variants([secret]):
        assert variant not in serialized
    assert serialized.count(REDACTION_MARKER) == 4


def test_redacting_log_writer_limits_utf8_content_and_marks_truncation() -> None:
    handle = io.StringIO()
    writer = RedactingLogWriter(handle, ["secret-token"], content_byte_limit=9)

    writer.feed("ääsecret-")
    writer.feed("token" + ("x" * 1000))
    writer.finish()
    result = handle.getvalue()
    retained = result.removesuffix(TRUNCATION_MARKER)

    assert writer.truncated is True
    assert len(retained.encode("utf-8")) <= 9
    assert "secret-token" not in result
    assert result.endswith(TRUNCATION_MARKER)


def test_redacting_log_writer_preserves_small_output_without_marker() -> None:
    handle = io.StringIO()
    writer = RedactingLogWriter(handle, ["secret-token"], content_byte_limit=1024)

    writer.feed("before secret-")
    writer.feed("token after")
    writer.finish()

    assert writer.truncated is False
    assert handle.getvalue() == f"before {REDACTION_MARKER} after"


def test_truncated_log_keeps_initial_report_metadata_parseable(tmp_path: Path) -> None:
    log_path = tmp_path / "agent.log"
    metadata = json.dumps(
        {
            "event": "eval_start",
            "agent_cli_version": "1.2.3",
        }
    )
    with log_path.open("w", encoding="utf-8") as handle:
        writer = RedactingLogWriter(handle, [], content_byte_limit=len(metadata.encode("utf-8")) + 1)
        writer.feed(metadata + "\n")
        writer.feed("unbounded output" * 100)
        writer.finish()

    assert extract_start_metadata(log_path)["agent_cli_version"] == "1.2.3"
    assert TRUNCATION_MARKER in log_path.read_text(encoding="utf-8")


def test_run_logged_redacts_encoded_secret_and_drains_after_limit(tmp_path: Path) -> None:
    secret = 'oauth/"token value'
    script = (
        "import json\n"
        "from urllib.parse import quote\n"
        f"secret = {secret!r}\n"
        'print(json.dumps({"event": "eval_start", "agent_cli_version": "1.2.3"}))\n'
        'print(quote(secret, safe=""))\n'
        'print("x" * 10000)\n'
    )
    log_path = tmp_path / "agent.log"

    exit_code, timed_out, cleanup_error = run_logged(
        [sys.executable, "-c", script],
        log_path,
        timeout_seconds=10,
        secrets=[secret],
        log_content_byte_limit=256,
    )
    result = log_path.read_text(encoding="utf-8")

    assert exit_code == 0
    # None, not False: the second element now says why a run was stopped.
    assert timed_out is None
    assert cleanup_error is None
    assert secret not in result
    assert quote(secret, safe="") not in result
    assert TRUNCATION_MARKER in result
    assert extract_start_metadata(log_path)["agent_cli_version"] == "1.2.3"


def test_auth_values_include_file_content_tokens_and_resolved_host_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    auth_file = tmp_path / "operator-secrets" / "auth.json"
    auth_file.parent.mkdir()
    token = "file-oauth-secret"
    auth_file.write_text(json.dumps({"access_token": token}), encoding="utf-8")
    monkeypatch.setattr(install_runner, "REPOSITORY_ROOT", repository)
    monkeypatch.setenv("CODEX_AUTH_FILE", str(auth_file))
    job = install_runner.Job(
        agent="codex",
        model="test",
        repetition=1,
        timeout_seconds=60,
        credentials=(),
        credential_files=(CredentialFile(kind="codex-auth", path_environment="CODEX_AUTH_FILE"),),
    )

    values = auth_values(job)

    assert token in values
    assert str(auth_file.resolve()) in values
    assert auth_file.resolve().as_posix() in values


def test_run_one_redacts_verifier_artifacts_failure_surfaces_and_host_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment_secret = 'env/"secret value'
    file_secret = "file-oauth-secret"
    auth_file = tmp_path / "operator-secrets" / "auth.json"
    auth_file.parent.mkdir()
    auth_file.write_text(json.dumps({"access_token": file_secret}), encoding="utf-8")
    fake_repository = tmp_path / "repository"
    fake_repository.mkdir()
    monkeypatch.setattr(install_runner, "REPOSITORY_ROOT", fake_repository)
    monkeypatch.setenv("OPENAI_API_KEY", environment_secret)
    monkeypatch.setenv("CODEX_AUTH_FILE", str(auth_file))

    matrix = install_runner.load_matrix(REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json")
    job = replace(
        matrix.jobs[0],
        credentials=("OPENAI_API_KEY",),
        credential_files=(CredentialFile(kind="codex-auth", path_environment="CODEX_AUTH_FILE"),),
    )
    matrix = replace(matrix, jobs=(job,))
    case = matrix.cases[0]
    output_root = tmp_path / "output"
    output_root.mkdir()

    monkeypatch.setattr(install_runner, "create_volume", lambda _docker, _name: None)
    monkeypatch.setattr(install_runner, "remove_container", lambda _docker, _name: None)
    monkeypatch.setattr(install_runner, "remove_volume", lambda _docker, _name: None)

    def fake_run_logged(
        _command: list[str],
        log_path: Path,
        **_kwargs: object,
    ) -> tuple[int, bool, str | None]:
        log_path.write_text(
            json.dumps({"event": "eval_start", "agent_cli_version": "test"}) + "\n",
            encoding="utf-8",
        )
        return 0, False, None

    verifier_payload = {
        "schema_version": 1,
        "ok": True,
        "checks": [
            {"detail": quote(environment_secret, safe="")},
            {"detail": json.dumps(file_secret)[1:-1]},
            {"detail": quote(str(auth_file.resolve()), safe="")},
        ],
    }

    def fake_run_capture(command: list[str], _timeout_seconds: int = 120) -> subprocess.CompletedProcess[str]:
        if "evals.install.scrub_credentials" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(verifier_payload) + "\n",
            f"stderr={quote_plus(environment_secret, safe='')};path={auth_file.resolve()}",
        )

    monkeypatch.setattr(install_runner, "run_logged", fake_run_logged)
    monkeypatch.setattr(install_runner, "run_capture", fake_run_capture)

    result = install_runner.run_one(
        docker="docker",
        matrix=matrix,
        case=case,
        job=job,
        source_root=REPOSITORY_ROOT,
        host_source_root=REPOSITORY_ROOT,
        output_root=output_root,
        image_id="sha256:test",
    )
    run_directory = output_root / result["id"]
    serialized_artifacts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            run_directory / "agent.log",
            run_directory / "job.json",
            run_directory / "verification.json",
            run_directory / "verifier.stderr.log",
            run_directory / "result.json",
        )
    )

    assert result["status"] == "passed"
    for secret in (environment_secret, file_secret, str(auth_file.resolve()), auth_file.resolve().as_posix()):
        for variant in secret_variants([secret]):
            assert variant not in serialized_artifacts
    assert REDACTION_MARKER in serialized_artifacts
