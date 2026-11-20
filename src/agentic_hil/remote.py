"""`agentic-hil remote`: a thin client of a shared bench's HTTP API (#891).

It reads no Agentic HIL configuration: a machine that only submits has none.
It speaks the bench server's API and nothing else, over the standard library's
HTTP client.

`submit` sends the plan and every artifact it names in one submission, and
keeps the job's token in `.agentic-hil/remote/<job_id>.json` for `withdraw`
and `fetch`. The token never reaches a URL, a log line or what the command
prints. With `--wait` it follows the job by long poll to its end and answers
with the run's report, so its exit code is the run's own verdict, as a local
`test-reactor` gives it. A refusal from the bench is passed on as the bench
wrote it.
"""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from agentic_hil.config import ConfigError, is_path_within_frozen
from agentic_hil.jobstore import TERMINAL_JOB_STATES, validated_job_id
from agentic_hil.scheduler import plan_artifact_paths
from agentic_hil.test_reactor import load_test_config
from agentic_hil.types import JsonObject

# Where the bench's address comes from when `--bench` names none.
BENCH_URL_ENV = "AGENTIC_HIL_BENCH_URL"
# Where a submission's token is kept, relative to the directory it was submitted from.
TOKEN_DIRECTORY = Path(".agentic-hil") / "remote"
# How long one status read asks the bench to hold it. The bench caps it at its own limit.
WAIT_S = 60
# How long one request may take before the bench counts as unreachable: a long poll's wait, and some.
REQUEST_TIMEOUT_S = WAIT_S + 30
# Between two status reads of a bench that answered without a validator to wait on.
POLL_INTERVAL_S = 1.0
# The name the plan part carries, as the plan is named in a workspace.
PLAN_FILENAME = "testconfig.yaml"


def check_arguments(parser: argparse.ArgumentParser, parsed: argparse.Namespace) -> None:
    """Settle the bench's address and the options that depend on each other, with argparse's own usage errors."""
    bench = parsed.bench or os.environ.get(BENCH_URL_ENV) or None
    if not bench:
        parser.error(f"the following arguments are required: --bench (or set {BENCH_URL_ENV})")
    split = urlsplit(bench)
    if split.scheme != "http" or not split.hostname or not _has_valid_port(bench):
        parser.error(f"--bench takes the http:// address of a shared bench, such as http://<bench>:8765, not {bench!r}")
    if getattr(parsed, "junit_xml", None) is not None and not getattr(parsed, "wait", False):
        parser.error("--junit-xml needs --wait: the JUnit file is the one the ended run gives")
    user = getattr(parsed, "user", None)
    if user is not None and not _sendable(user):
        parser.error("--user takes a printable name of Latin-1 characters, which an HTTP From header can carry")
    parsed.bench = bench.rstrip("/")


def run(args: argparse.Namespace) -> JsonObject:
    """Answer one `agentic-hil remote` action."""
    bench = str(args.bench)
    action = args.remote_command
    if action == "submit":
        return _submit(bench, str(args.plan), wait=bool(args.wait), junit_xml=args.junit_xml, user=args.user)
    if action == "status":
        return _status(bench, str(args.job))
    if action == "queue":
        return _queue(bench)
    if action == "withdraw":
        return _withdraw(bench, str(args.job))
    return _fetch(bench, str(args.job), str(args.into))


# -- Refusals of this client's own ---------------------------------------------


def _unreachable(bench: str, reason: str) -> ConfigError:
    return ConfigError(
        "bench_unreachable",
        "The shared bench did not answer, or answered with something that is not its API's JSON.",
        {"bench": bench, "backend_error": reason},
    )


def _missing_artifact(relative: str, plan: str) -> ConfigError:
    return ConfigError(
        "remote_artifact_missing",
        f"The plan names {relative!r}, and this machine has no such file inside the directory the plan is submitted from, so nothing was sent.",
        {"artifact": relative, "plan": plan},
    )


def _stored_token(job_id: str) -> str:
    path = _token_path(job_id)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = None
    token = stored.get("job_token") if isinstance(stored, dict) else None
    if not isinstance(token, str) or not token:
        raise ConfigError(
            "job_token_missing",
            "This machine holds no token for that job: only the directory it was submitted from can withdraw it or fetch what it produced.",
            {"job_id": job_id, "token_file": str(path)},
        )
    return token


# -- Talking to the bench --------------------------------------------------------


@dataclass(frozen=True)
class _Answer:
    status: int
    headers: dict[str, str]
    body: bytes


def _request(bench: str, method: str, path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None) -> _Answer:
    split = urlsplit(bench)
    connection = http.client.HTTPConnection(split.hostname or "", split.port or 80, timeout=REQUEST_TIMEOUT_S)
    try:
        connection.request(method, split.path.rstrip("/") + path, body=body, headers=headers or {})
        response = connection.getresponse()
        return _Answer(response.status, {name.lower(): value for name, value in response.getheaders()}, response.read())
    except (OSError, http.client.HTTPException) as error:
        raise _unreachable(bench, str(error) or type(error).__name__) from error
    finally:
        connection.close()


def _document(bench: str, answer: _Answer) -> JsonObject:
    try:
        value = json.loads(answer.body)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise _unreachable(bench, f"the bench answered HTTP {answer.status} without a JSON document")
    return value


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _job_path(job_id: str) -> str:
    return "/v1/jobs/" + quote(job_id, safe="")


def _has_valid_port(bench: str) -> bool:
    """Whether the address's port, when it names one, is a port number."""
    try:
        port = urlsplit(bench).port
    except ValueError:
        return False
    return port is None or port > 0


def _sendable(name: str) -> bool:
    try:
        name.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return bool(name.strip()) and name.isprintable()


def _submitter(user: str | None) -> str | None:
    """`--user`, or this machine's login name; None where neither can be sent, and the bench says anonymous."""
    if user is not None:
        return user
    try:
        name = getpass.getuser()
    except Exception:
        return None
    return name if isinstance(name, str) and _sendable(name) else None


# -- The token file ----------------------------------------------------------------


def _token_path(job_id: str) -> Path:
    return Path.cwd() / TOKEN_DIRECTORY / f"{validated_job_id(job_id)}.json"


def _keep_token(bench: str, job_id: str, token: str) -> Path:
    """Write the job's token where withdraw and fetch read it, readable by this account alone where the platform says so."""
    path = _token_path(job_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    text = json.dumps({"bench": bench, "job_id": job_id, "job_token": token}, indent=2) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


# -- The actions -------------------------------------------------------------------


def _multipart(boundary: str, plan: bytes, artifacts: dict[str, bytes]) -> bytes:
    """One form-data submission: the plan part, then each artifact under the path the plan names it by."""
    parts = [("plan", PLAN_FILENAME, "application/yaml", plan)]
    parts.extend((name, PurePosixPath(name).name, "application/octet-stream", data) for name, data in artifacts.items())
    chunks: list[bytes] = []
    for name, filename, media_type, data in parts:
        quoted_name = name.replace("\\", "\\\\").replace('"', '\\"')
        quoted_file = filename.replace("\\", "\\\\").replace('"', '\\"')
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{quoted_name}"; filename="{quoted_file}"\r\nContent-Type: {media_type}\r\n\r\n'.encode()
        )
        chunks.extend((data, b"\r\n"))
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)


def _artifact(workspace: Path, relative: str, plan: str) -> bytes:
    path = workspace / relative
    if not is_path_within_frozen(path, workspace) or not path.is_file():
        raise _missing_artifact(relative, plan)
    return path.read_bytes()


def _submit(bench: str, plan: str, *, wait: bool, junit_xml: str | None, user: str | None) -> JsonObject:
    workspace = Path.cwd()
    loaded = load_test_config(plan, str(workspace))
    plan_bytes = Path(loaded.path).read_bytes()
    # Every file the plan names, read before anything is sent: a missing one sends nothing.
    artifacts = {relative: _artifact(workspace, relative, plan) for relative in plan_artifact_paths(loaded.steps)}
    boundary = f"agentic-hil-remote-{secrets.token_hex(16)}"
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    submitter = _submitter(user)
    if submitter is not None:
        headers["From"] = submitter
    answer = _request(bench, "POST", "/v1/jobs", body=_multipart(boundary, plan_bytes, artifacts), headers=headers)
    document = _document(bench, answer)
    if answer.status != 202:
        return document
    job_id = str(document.get("job_id"))
    token = document.get("job_token")
    if not isinstance(token, str) or not token:
        raise _unreachable(bench, "the bench accepted the submission and answered no job token for it")
    kept = _keep_token(bench, job_id, token)
    submitted = {key: value for key, value in document.items() if key != "job_token"}
    if not wait:
        return {"ok": True, "tool": "remote_submit", "bench": bench, **submitted, "saved_to": str(kept)}
    return _until_ended(bench, job_id, token, junit_xml)


def _until_ended(bench: str, job_id: str, token: str, junit_xml: str | None) -> JsonObject:
    """Follow the job by long poll to its end, then answer with its report and write its JUnit file."""
    path = _job_path(job_id)
    validator: str | None = None
    while True:
        headers = {"Prefer": f"wait={WAIT_S}"}
        if validator is not None:
            headers["If-None-Match"] = validator
        answer = _request(bench, "GET", path, headers=headers)
        if answer.status == 304:
            continue
        status = _document(bench, answer)
        if answer.status != 200:
            return status
        if status.get("state") in TERMINAL_JOB_STATES:
            break
        validator = answer.headers.get("etag")
        if validator is None:
            time.sleep(POLL_INTERVAL_S)
    answered = _request(bench, "GET", path + "/report", headers=_bearer(token))
    report = _document(bench, answered)
    if answered.status != 200 or junit_xml is None:
        return report
    junit = _request(bench, "GET", path + "/junit", headers=_bearer(token))
    if junit.status != 200:
        return _document(bench, junit)
    target = Path(junit_xml)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(junit.body)
    return report


def _status(bench: str, job_id: str) -> JsonObject:
    answer = _request(bench, "GET", _job_path(job_id))
    status = _document(bench, answer)
    if answer.status != 200:
        return status
    # The command answered; the job's own verdict, null until it ends, is `job_ok`.
    return {"ok": True, "tool": "remote_status", "bench": bench, **{key: value for key, value in status.items() if key != "ok"}, "job_ok": status.get("ok")}


def _queue(bench: str) -> JsonObject:
    answer = _request(bench, "GET", "/v1/queue")
    queue = _document(bench, answer)
    if answer.status != 200:
        return queue
    return {"ok": True, "tool": "remote_queue", "bench": bench, "entries": queue.get("entries", [])}


def _withdraw(bench: str, job_id: str) -> JsonObject:
    token = _stored_token(job_id)
    answer = _request(bench, "DELETE", _job_path(job_id), headers=_bearer(token))
    if answer.status == 204:
        return {"ok": True, "tool": "remote_withdraw", "bench": bench, "job_id": job_id, "state": "withdrawn", "started": False}
    withdrawn = _document(bench, answer)
    if answer.status != 202:
        return withdrawn
    return {"ok": True, "tool": "remote_withdraw", "bench": bench, **withdrawn}


def _served_name(disposition: str | None, fallback: str) -> str:
    """The basename the bench served a file under, read from its Content-Disposition."""
    marker = "filename*=UTF-8''"
    if disposition and marker in disposition:
        name = Path(unquote(disposition.split(marker, 1)[1].split(";", 1)[0].strip())).name
        if name not in {"", ".", ".."}:
            return name
    return fallback


def _fetch(bench: str, job_id: str, into: str) -> JsonObject:
    token = _stored_token(job_id)
    path = _job_path(job_id)
    answered = _request(bench, "GET", path + "/report", headers=_bearer(token))
    report = _document(bench, answered)
    if answered.status != 200:
        return report
    junit = _request(bench, "GET", path + "/junit", headers=_bearer(token))
    if junit.status != 200:
        return _document(bench, junit)
    evidence = _request(bench, "GET", path + "/evidence", headers=_bearer(token))
    if evidence.status != 200:
        return _document(bench, evidence)
    directory = Path(into)
    directory.mkdir(parents=True, exist_ok=True)
    written = {
        "report.json": (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        "junit.xml": junit.body,
        _served_name(evidence.headers.get("content-disposition"), f"{job_id}-evidence.zip"): evidence.body,
    }
    for name, content in written.items():
        (directory / name).write_bytes(content)
    return {
        "ok": True,
        "tool": "remote_fetch",
        "bench": bench,
        "job_id": job_id,
        "into": str(directory),
        "files": sorted(written),
        "job_ok": report.get("ok"),
    }
