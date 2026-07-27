from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Case, CredentialFile, Job, Matrix, load_matrix
from .credentials import authentication_failure, credential_health
from .redaction import DEFAULT_LOG_CONTENT_BYTES, RedactingLogWriter, redact, redact_value
from .refresh_login import apply_refreshed_login
from .report import report_results, result_group, unstable_groups
from .routing import routing_results
from .source import create_source_snapshot, source_digest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DOCKERFILE = REPOSITORY_ROOT / "evals" / "install" / "container" / "Dockerfile"
TOOL_CONTRACT = Path("harness") / "guest" / "tools.list.expected"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip(".-") or "run"
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{cleaned[:61]}-{suffix}"


def git_metadata(source_root: Path) -> dict[str, Any]:
    try:
        revision = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(source_root), "status", "--porcelain"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=True,
            ).stdout.strip()
        )
        return {"revision": revision, "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"revision": None, "dirty": None}


def validate_source_version(source_root: Path, expected_version: str) -> None:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
        import tomli as tomllib

    data = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if version != expected_version:
        raise ValueError(f"source version {version!r} does not match target.expected_version {expected_version!r}")


def validate_remote_checkout(source_root: Path, expected_commit: str) -> None:
    metadata = git_metadata(source_root)
    revision = metadata.get("revision")
    if not isinstance(revision, str) or revision.lower() != expected_commit.lower():
        raise ValueError(
            f"remote evaluation requires --source-root checked out at {expected_commit}; found {revision or '<unknown>'}"
        )
    result = subprocess.run(
        [
            "git",
            "-C",
            str(source_root),
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "pyproject.toml",
            "src/agentic_hil",
            str(TOOL_CONTRACT),
        ],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "could not verify remote source checkout")
    if result.stdout.strip():
        raise ValueError(
            "remote evaluation requires clean pyproject.toml, src/agentic_hil, "
            "and harness/guest/tools.list.expected at expected commit"
        )


def expected_mcp_tools(source_root: Path) -> list[str]:
    contract = source_root / TOOL_CONTRACT
    if contract.is_symlink() or not contract.is_file():
        raise ValueError(f"target MCP tool contract must be a regular non-symlink file: {contract}")
    names = [line.strip() for line in contract.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not names:
        raise ValueError(f"target MCP tool contract is empty: {contract}")
    if names != sorted(names) or len(names) != len(set(names)):
        raise ValueError(f"target MCP tool contract must be sorted and unique: {contract}")
    invalid = [name for name in names if re.fullmatch(r"[a-z][a-z0-9_]*", name) is None]
    if invalid:
        raise ValueError(f"target MCP tool contract contains invalid names: {invalid}")
    return names


def job_payload(
    matrix: Matrix,
    case: Case,
    job: Job,
    source_root: Path | None,
    host_source_root: Path,
) -> dict[str, Any]:
    target = dataclasses.asdict(matrix.target)
    evidence_root = source_root if matrix.target.mode == "local" else host_source_root
    if evidence_root is None:
        raise ValueError("target requires trusted source evidence")
    target["expected_package_digest"] = source_digest(evidence_root / "src" / "agentic_hil")
    target["source_git"] = git_metadata(host_source_root)
    tool_names = expected_mcp_tools(host_source_root)
    target["expected_mcp_tools"] = tool_names
    target["expected_mcp_tools_digest"] = hashlib.sha256(
        ("\n".join(tool_names) + "\n").encode("utf-8")
    ).hexdigest()
    if matrix.target.mode == "local":
        if source_root is None:
            raise ValueError("local target requires source_root")
        target["source_digest"] = source_digest(source_root)
    return {
        "schema_version": 1,
        "executor": "docker",
        "agent": job.agent,
        "model": job.model,
        "repetition": job.repetition,
        "timeout_seconds": job.timeout_seconds,
        "credential_file_kinds": [credential.kind for credential in job.credential_files],
        "case": dataclasses.asdict(case),
        "target": target,
    }


def docker_security_options() -> list[str]:
    return [
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "512",
        "--memory",
        "4g",
        "--cpus",
        "2",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,nodev,size=1g",
    ]


def docker_mount(kind: str, source: str, destination: str, *, readonly: bool = False) -> str:
    parts = [f"type={kind}", f"src={source}", f"dst={destination}"]
    if readonly:
        parts.append("readonly")
    return ",".join(parts)


def agent_container_command(
    *,
    docker: str,
    image: str,
    container_name: str,
    home_volume: str,
    workspace_volume: str,
    job_file: Path,
    source_root: Path | None,
    credentials: tuple[str, ...],
    credential_files: tuple[tuple[str, Path], ...] = (),
) -> list[str]:
    command = [
        docker,
        "run",
        "--rm",
        "--name",
        container_name,
        *docker_security_options(),
        "--mount",
        docker_mount("volume", home_volume, "/home/eval"),
        "--mount",
        docker_mount("volume", workspace_volume, "/workspace"),
        "--mount",
        docker_mount("bind", str(job_file.resolve()), "/job.json", readonly=True),
    ]
    if source_root is not None:
        command.extend(
            [
                "--mount",
                docker_mount("bind", str(source_root.resolve()), "/mnt/source", readonly=True),
            ]
        )
    for kind, path in credential_files:
        command.extend(
            [
                "--mount",
                docker_mount(
                    "bind",
                    str(path),
                    f"/run/agentic-hil-secrets/{kind}",
                    readonly=True,
                ),
            ]
        )
    for name in credentials:
        if os.environ.get(name):
            command.extend(["--env", name])
    return [*command, image, "--job", "/job.json"]


def verifier_container_command(
    *,
    docker: str,
    image: str,
    container_name: str,
    home_volume: str,
    workspace_volume: str,
    job_file: Path,
) -> list[str]:
    return [
        docker,
        "run",
        "--rm",
        "--name",
        container_name,
        *docker_security_options(),
        "--network",
        "none",
        "--workdir",
        "/opt",
        "--mount",
        docker_mount("volume", home_volume, "/home/eval", readonly=True),
        "--mount",
        docker_mount("volume", workspace_volume, "/workspace", readonly=True),
        "--mount",
        docker_mount("bind", str(job_file.resolve()), "/job.json", readonly=True),
        "--entrypoint",
        "/opt/install-eval-verifier/bin/python",
        image,
        "-m",
        "evals.install.verifier",
        "--job",
        "/job.json",
    ]


def credential_export_command(
    *,
    docker: str,
    image: str,
    container_name: str,
    home_volume: str,
    kinds: tuple[str, ...],
) -> list[str]:
    """Read the staged logins out of the home volume, without network or write access."""
    return [
        docker,
        "run",
        "--rm",
        "--name",
        container_name,
        *docker_security_options(),
        "--network",
        "none",
        "--workdir",
        "/opt",
        "--mount",
        docker_mount("volume", home_volume, "/home/eval", readonly=True),
        "--entrypoint",
        "/usr/bin/python3",
        image,
        "-m",
        "evals.install.refresh_login",
        "--kinds",
        *kinds,
    ]


def credential_scrubber_command(
    *,
    docker: str,
    image: str,
    container_name: str,
    home_volume: str,
    kinds: tuple[str, ...],
) -> list[str]:
    return [
        docker,
        "run",
        "--rm",
        "--name",
        container_name,
        *docker_security_options(),
        "--network",
        "none",
        "--workdir",
        "/opt",
        "--mount",
        docker_mount("volume", home_volume, "/home/eval"),
        "--entrypoint",
        "/usr/bin/python3",
        image,
        "-m",
        "evals.install.scrub_credentials",
        "--kinds",
        *kinds,
    ]


def docker_subprocess_environment(docker: str) -> dict[str, str]:
    """Keep Docker Desktop's sibling credential helpers discoverable."""
    environment = os.environ.copy()
    docker_path = Path(docker)
    if not docker_path.is_absolute():
        return environment

    docker_directory = str(docker_path.parent)
    path_key = next((key for key in environment if key.casefold() == "path"), "PATH")
    path_entries = [entry for entry in environment.get(path_key, "").split(os.pathsep) if entry]
    normalized_directory = os.path.normcase(os.path.normpath(docker_directory))
    if all(
        os.path.normcase(os.path.normpath(entry.strip('"'))) != normalized_directory
        for entry in path_entries
    ):
        path_entries.insert(0, docker_directory)
    environment[path_key] = os.pathsep.join(path_entries)
    return environment


def run_logged(
    command: list[str],
    log_path: Path,
    *,
    timeout_seconds: int,
    secrets: list[str],
    timeout_container: str | None = None,
    log_content_byte_limit: int = DEFAULT_LOG_CONTENT_BYTES,
) -> tuple[int, bool, str | None]:
    process = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=64 * 1024,
        errors="replace",
        env=docker_subprocess_environment(command[0]),
    )
    chunks: queue.Queue[str | None] = queue.Queue(maxsize=16)

    def reader() -> None:
        assert process.stdout is not None
        try:
            while chunk := process.stdout.read(64 * 1024):
                chunks.put(chunk)
        finally:
            chunks.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    started = time.monotonic()
    timed_out = False
    timeout_cleanup_error: str | None = None
    stream_closed = False
    with log_path.open("w", encoding="utf-8") as handle:
        log_writer = RedactingLogWriter(handle, secrets, content_byte_limit=log_content_byte_limit)
        while not stream_closed or process.poll() is None:
            if not timed_out and process.poll() is None and time.monotonic() - started > timeout_seconds:
                timed_out = True
                if timeout_container:
                    try:
                        remove_container(command[0], timeout_container)
                    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                        timeout_cleanup_error = (
                            f"container {timeout_container}: timeout removal failed: "
                            f"{type(error).__name__}: {error}"
                        )
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            try:
                item = chunks.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                stream_closed = True
                continue
            log_writer.feed(item)
        log_writer.finish()
    return process.wait(), timed_out, timeout_cleanup_error


def run_capture(command: list[str], timeout_seconds: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
        env=docker_subprocess_environment(command[0]),
    )


def extract_start_metadata(log_path: Path) -> dict[str, Any]:
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("event") == "eval_start":
            return value
    return {}


def create_volume(docker: str, name: str) -> None:
    result = run_capture([docker, "volume", "create", "--label", "agentic-hil.eval=true", name], 30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"could not create Docker volume {name}")


def _docker_resource_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    detail = f"{result.stdout}\n{result.stderr}".lower()
    return "no such" in detail or "not found" in detail


def _docker_resource_exists(docker: str, kind: str, name: str) -> bool:
    result = run_capture([docker, kind, "inspect", name], 30)
    if result.returncode == 0:
        return True
    if _docker_resource_not_found(result):
        return False
    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    raise RuntimeError(f"could not inspect Docker {kind} {name}: {detail}")


def _remove_docker_resource(docker: str, kind: str, name: str) -> None:
    if not _docker_resource_exists(docker, kind, name):
        return
    result = run_capture([docker, kind, "rm", "--force", name], 30)
    if result.returncode != 0 and not _docker_resource_not_found(result):
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"could not remove Docker {kind} {name}: {detail}")
    if _docker_resource_exists(docker, kind, name):
        raise RuntimeError(f"Docker {kind} {name} still exists after removal")


def remove_volume(docker: str, name: str) -> None:
    _remove_docker_resource(docker, "volume", name)


def remove_container(docker: str, name: str) -> None:
    _remove_docker_resource(docker, "container", name)


def image_digest(docker: str, image: str) -> str | None:
    result = run_capture([docker, "image", "inspect", "--format", "{{.Id}}", image], 30)
    return result.stdout.strip() if result.returncode == 0 else None


def parse_verification(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    for line in reversed(result.stdout.splitlines()):
        with_json = line.strip()
        if not with_json:
            continue
        try:
            value = json.loads(with_json)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("schema_version") == 1:
            return value
    raise ValueError(result.stderr.strip() or "verifier did not return JSON")


def credential_file_path(credential: CredentialFile) -> Path:
    raw = os.environ.get(credential.path_environment)
    if not raw:
        raise ValueError(f"missing credential file environment: {credential.path_environment}")
    configured = Path(raw).expanduser()
    if not configured.is_absolute():
        raise ValueError(f"{credential.path_environment} must contain an absolute file path")
    try:
        path = configured.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"{credential.path_environment} cannot be resolved: {error}") from error
    if not path.is_file():
        raise ValueError(f"{credential.path_environment} does not identify a regular file")
    try:
        path.relative_to(REPOSITORY_ROOT.resolve())
    except ValueError:
        pass
    else:
        raise ValueError(f"{credential.path_environment} must point outside the repository")
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError(f"{credential.path_environment} exceeds 2 MiB")
    if "," in str(path):
        raise ValueError(f"{credential.path_environment} path may not contain a comma")
    return path


def resolved_credential_files(job: Job) -> tuple[tuple[str, Path], ...]:
    return tuple((credential.kind, credential_file_path(credential)) for credential in job.credential_files)


def _secret_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if len(value) >= 8 else []
    if isinstance(value, list):
        return [secret for item in value for secret in _secret_strings(item)]
    if isinstance(value, dict):
        return [secret for item in value.values() for secret in _secret_strings(item)]
    return []


def auth_values(job: Job) -> list[str]:
    values = [os.environ[name] for name in job.credentials if os.environ.get(name)]
    for _kind, path in resolved_credential_files(job):
        content = path.read_text(encoding="utf-8", errors="replace")
        values.extend((str(path), path.as_posix(), content))
        with contextlib.suppress(json.JSONDecodeError):
            values.extend(_secret_strings(json.loads(content)))
    return list(dict.fromkeys(value for value in values if value))


AUTHENTICATION_LOG_CHARS = 8000


def agent_authentication_failure(run_directory: Path) -> str | None:
    """Whether the agent CLI refused to start because its login is unusable."""
    log_path = run_directory / "agent.log"
    if not log_path.is_file():
        return None
    text = log_path.read_text(encoding="utf-8", errors="replace")[-AUTHENTICATION_LOG_CHARS:]
    return authentication_failure(text)


def ensure_auth(job: Job) -> None:
    missing = [name for name in job.credentials if not os.environ.get(name)]
    if missing:
        names = ", ".join(missing)
        raise ValueError(f"{job.agent}/{job.model}: missing configured credential environment: {names}")
    resolved_credential_files(job)


def run_one(
    *,
    docker: str,
    matrix: Matrix,
    case: Case,
    job: Job,
    source_root: Path | None,
    host_source_root: Path,
    output_root: Path,
    image_id: str | None,
    refresh_login: bool = False,
) -> dict[str, Any]:
    run_id = slug(f"{case.id}-{job.agent}-{job.model}-r{job.repetition}")
    run_directory = output_root / run_id
    if run_directory.exists():
        raise FileExistsError(f"run output already exists: {run_directory}")
    run_directory.mkdir(parents=True)
    payload = job_payload(matrix, case, job, source_root, host_source_root)
    job_file = run_directory / "job.json"
    job_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    token = uuid.uuid4().hex[:12]
    home_volume = f"agentic-hil-eval-{token}-home"
    workspace_volume = f"agentic-hil-eval-{token}-workspace"
    agent_container = f"agentic-hil-eval-{token}-agent"
    scrubber_container = f"agentic-hil-eval-{token}-scrub"
    verifier_container = f"agentic-hil-eval-{token}-verify"
    credential_files = resolved_credential_files(job)
    redaction_secrets = [os.environ[name] for name in job.credentials if os.environ.get(name)]
    for _kind, path in credential_files:
        redaction_secrets.extend((str(path), path.as_posix()))
    credentials_scrubbed = not credential_files
    created_volumes: list[str] = []
    cleanup_errors: list[str] = []
    cleanup_resources: set[str] = set()
    unresolved_containers: set[str] = set()
    unresolved_volumes: set[str] = set()
    started_at = utc_now()
    started = time.monotonic()
    status = "error"
    failure: str | None = None
    agent_exit_code: int | None = None
    verifier_exit_code: int | None = None
    verification: dict[str, Any] | None = None

    def record_cleanup_error(kind: str, name: str, error: object) -> None:
        cleanup_resources.add(name)
        cleanup_errors.append(f"{kind} {name}: {type(error).__name__}: {error}")

    try:
        redaction_secrets = list(dict.fromkeys((*redaction_secrets, *auth_values(job))))
        create_volume(docker, home_volume)
        created_volumes.append(home_volume)
        create_volume(docker, workspace_volume)
        created_volumes.append(workspace_volume)
        agent_command = agent_container_command(
            docker=docker,
            image=matrix.image,
            container_name=agent_container,
            home_volume=home_volume,
            workspace_volume=workspace_volume,
            job_file=job_file,
            source_root=source_root if matrix.target.mode == "local" else None,
            credentials=job.credentials,
            credential_files=credential_files,
        )
        agent_exit_code, timed_out, timeout_cleanup_error = run_logged(
            agent_command,
            run_directory / "agent.log",
            timeout_seconds=job.timeout_seconds,
            secrets=redaction_secrets,
            timeout_container=agent_container,
        )
        if timeout_cleanup_error:
            cleanup_resources.add(agent_container)
            cleanup_errors.append(timeout_cleanup_error)
            unresolved_containers.add(agent_container)
            raise RuntimeError(timeout_cleanup_error)
        if timed_out:
            failure = f"agent timed out after {job.timeout_seconds}s"

        if credential_files and refresh_login:
            # The agent CLI may have refreshed a token; collect it before the
            # scrubber removes every copy, and put it back where it came from.
            export_result = run_capture(
                credential_export_command(
                    docker=docker,
                    image=matrix.image,
                    container_name=f"{agent_container}-export",
                    home_volume=home_volume,
                    kinds=tuple(kind for kind, _path in credential_files),
                ),
                60,
            )
            if export_result.returncode == 0:
                with contextlib.suppress(ValueError):
                    returned = json.loads(export_result.stdout or "{}")
                    for kind, path in credential_files:
                        content = returned.get(kind)
                        if not isinstance(content, str) or not content.strip():
                            continue
                        redaction_secrets = list(dict.fromkeys((*redaction_secrets, content)))
                        print(f"{kind}: {apply_refreshed_login(kind, path, content)}", file=sys.stderr)

        if credential_files:
            scrubber_command = credential_scrubber_command(
                docker=docker,
                image=matrix.image,
                container_name=scrubber_container,
                home_volume=home_volume,
                kinds=tuple(kind for kind, _path in credential_files),
            )
            scrubber_result = run_capture(scrubber_command, 60)
            if scrubber_result.returncode != 0:
                raise RuntimeError(scrubber_result.stderr.strip() or "credential scrubber failed")
            credentials_scrubbed = True

        verifier_command = verifier_container_command(
            docker=docker,
            image=matrix.image,
            container_name=verifier_container,
            home_volume=home_volume,
            workspace_volume=workspace_volume,
            job_file=job_file,
        )
        verifier_result = run_capture(verifier_command, 180)
        verifier_exit_code = verifier_result.returncode
        with (run_directory / "verifier.stderr.log").open("w", encoding="utf-8") as handle:
            verifier_log = RedactingLogWriter(handle, redaction_secrets)
            verifier_log.feed(verifier_result.stderr)
            verifier_log.finish()
        verification = parse_verification(verifier_result)
        verification_for_report = redact_value(verification, redaction_secrets)
        (run_directory / "verification.json").write_text(
            json.dumps(verification_for_report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        expected_safe_failure = case.expected_outcome == "safe-failure"
        agent_ok = agent_exit_code == 0 or expected_safe_failure
        if not timed_out and agent_ok and verifier_exit_code == 0 and verification.get("ok") is True:
            status = "passed"
        elif agent_exit_code != 0 and (phrase := agent_authentication_failure(run_directory)):
            # A dead login is not a result about the installation guide.
            status = "error"
            failure = f"agent could not authenticate ({phrase}); the stored login needs to be renewed"
        else:
            status = "failed"
            failure = failure or "agent or independent verification failed"
    except Exception as error:
        failure = redact(f"{type(error).__name__}: {error}", redaction_secrets)
    finally:
        for container in (agent_container, verifier_container, scrubber_container):
            try:
                remove_container(docker, container)
                unresolved_containers.discard(container)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                unresolved_containers.add(container)
                record_cleanup_error("container", container, error)
        if (
            credential_files
            and not credentials_scrubbed
            and home_volume in created_volumes
            and agent_container not in unresolved_containers
        ):
            try:
                scrubber_result = run_capture(
                    credential_scrubber_command(
                        docker=docker,
                        image=matrix.image,
                        container_name=scrubber_container,
                        home_volume=home_volume,
                        kinds=tuple(kind for kind, _path in credential_files),
                    ),
                    60,
                )
                credentials_scrubbed = scrubber_result.returncode == 0
                if not credentials_scrubbed:
                    detail = scrubber_result.stderr.strip() or scrubber_result.stdout.strip() or "unknown error"
                    raise RuntimeError(detail)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                unresolved_volumes.add(home_volume)
                record_cleanup_error("credential volume", home_volume, error)
            finally:
                try:
                    remove_container(docker, scrubber_container)
                    unresolved_containers.discard(scrubber_container)
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    unresolved_containers.add(scrubber_container)
                    record_cleanup_error("container", scrubber_container, error)

        # Raw agent evidence may contain copied environment or file credentials.
        # Never retain it as a Docker volume.
        for volume in reversed(created_volumes):
            try:
                remove_volume(docker, volume)
                unresolved_volumes.discard(volume)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                unresolved_volumes.add(volume)
                record_cleanup_error("volume", volume, error)

        if cleanup_errors:
            status = "error"
            cleanup_failure = "cleanup failed: " + "; ".join(cleanup_errors)
            failure = f"{failure}; {cleanup_failure}" if failure else cleanup_failure

    result = {
        "schema_version": 1,
        "kind": "install",
        "executor": "docker",
        "id": run_id,
        "status": status,
        "agent": {
            "cli": job.agent,
            "model": job.model,
            "cli_version": extract_start_metadata(run_directory / "agent.log").get("agent_cli_version")
            if (run_directory / "agent.log").is_file()
            else None,
        },
        "case": {"id": case.id, "expected_outcome": case.expected_outcome},
        "target": payload["target"],
        "environment": {
            "image": matrix.image,
            "image_digest": image_id,
            "credential_file_links_scrubbed": credentials_scrubbed if credential_files else None,
            "home_volume": home_volume if home_volume in unresolved_volumes else None,
            "workspace_volume": workspace_volume if workspace_volume in unresolved_volumes else None,
        },
        "cleanup": {
            "ok": not cleanup_errors,
            "errors": cleanup_errors,
            "resources": sorted(cleanup_resources),
            "remaining_containers": sorted(unresolved_containers),
            "remaining_volumes": sorted(unresolved_volumes),
        },
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "agent_exit_code": agent_exit_code,
        "verifier_exit_code": verifier_exit_code,
        "failure": failure,
        "checks": verification.get("checks", []) if verification else [],
        "artifacts": {
            "agent_log": "agent.log",
            "job": "job.json",
            "verification": "verification.json" if verification else None,
            "verifier_stderr": "verifier.stderr.log",
        },
    }
    result = redact_value(result, redaction_secrets)
    (run_directory / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def dry_run_plan(
    matrix: Matrix,
    source_root: Path | None,
    host_source_root: Path,
    docker: str,
) -> dict[str, Any]:
    plans: list[dict[str, Any]] = []
    for case in matrix.cases:
        for job in matrix.jobs:
            payload = job_payload(matrix, case, job, source_root, host_source_root)
            placeholder = Path("/absolute/job.json")
            credential_file_placeholders = tuple(
                (
                    credential.kind,
                    Path(f"/HOST_SECRET_FROM_{credential.path_environment}"),
                )
                for credential in job.credential_files
            )
            plans.append(
                {
                    "case": case.id,
                    "agent": job.agent,
                    "model": job.model,
                    "repetition": job.repetition,
                    "payload": payload,
                    "forwarded_environment_names": [name for name in job.credentials if os.environ.get(name)],
                    "forwarded_credential_file_kinds": [credential.kind for credential in job.credential_files],
                    "agent_command": agent_container_command(
                        docker=docker,
                        image=matrix.image,
                        container_name="agentic-hil-eval-DRYRUN-agent",
                        home_volume="agentic-hil-eval-DRYRUN-home",
                        workspace_volume="agentic-hil-eval-DRYRUN-workspace",
                        job_file=placeholder,
                        source_root=source_root if matrix.target.mode == "local" else None,
                        credentials=job.credentials,
                        credential_files=credential_file_placeholders,
                    ),
                }
            )
    return {"schema_version": 1, "image": matrix.image, "runs": plans}


def run_matrix(args: argparse.Namespace) -> int:
    matrix = load_matrix(args.matrix)
    if args.image:
        matrix = dataclasses.replace(matrix, image=args.image)
    host_source_root = Path(args.source_root).resolve()
    validate_source_version(host_source_root, matrix.target.expected_version)
    if matrix.target.mode == "remote":
        assert matrix.target.expected_commit is not None
        validate_remote_checkout(host_source_root, matrix.target.expected_commit)
    docker = shutil.which(args.docker) or args.docker

    with contextlib.ExitStack() as stack:
        source_root: Path | None = None
        if matrix.target.mode == "local":
            temporary_root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="agentic-hil-eval-source-")))
            source_root = create_source_snapshot(host_source_root, temporary_root / "source")

        if args.dry_run:
            print(json.dumps(dry_run_plan(matrix, source_root, host_source_root, docker), indent=2, sort_keys=True))
            return 0
        if shutil.which(args.docker) is None:
            raise RuntimeError(f"Docker CLI not found: {args.docker}")
        for job in matrix.jobs:
            ensure_auth(job)
        # A login that can no longer be refreshed fails every run in the matrix,
        # so refuse before spending model budget on it.
        for kind, path in sorted({pair for job in matrix.jobs for pair in resolved_credential_files(job)}):
            state, detail = credential_health(kind, path)
            if state == "expired":
                raise RuntimeError(f"the {kind} login is unusable: {detail}. Sign in again, then rerun.")
            if state == "stale" and not args.refresh_login:
                raise RuntimeError(
                    f"the {kind} login would be refreshed inside the container: {detail}. Refreshing it "
                    f"there can rotate the token and leave this machine's copy rejected. Start the agent "
                    f"CLI once on this machine so it refreshes here, or pass --refresh-login to write the "
                    f"refreshed token back, then rerun."
                )
            print(
                f"WARNING: {kind} is a stored interactive login. Refreshing it inside the container can "
                f"rotate the token and invalidate this machine's copy; an API key or a token minted for "
                f"automation avoids that. State: {detail}",
                file=sys.stderr,
            )

        image_id = image_digest(docker, matrix.image)
        if image_id is None:
            raise RuntimeError(f"Docker image not found: {matrix.image}; run the build command first")
        output_root = Path(args.output).resolve()
        output_root.mkdir(parents=True, exist_ok=False)

        def execute(case: Case, job: Job) -> dict[str, Any]:
            return run_one(
                docker=docker,
                matrix=matrix,
                case=case,
                job=job,
                source_root=source_root,
                host_source_root=host_source_root,
                output_root=output_root,
                image_id=image_id,
                refresh_login=args.refresh_login,
            )

        results = []
        aborted: str | None = None
        for case in matrix.cases:
            for job in matrix.jobs:
                result = execute(case, job)
                results.append(result)
                # Every later run would fail the same way and cost the same.
                if result["status"] == "error" and "could not authenticate" in (result.get("failure") or ""):
                    aborted = str(result["failure"])
                    print(f"ERROR: {aborted}; stopping the matrix", file=sys.stderr)
                    break
            if aborted:
                break

        # Every run gets its own container and its own pair of volumes, so a
        # repetition never inherits state from the one before it.
        cases_by_id = {case.id: case for case in matrix.cases}
        templates: dict[tuple[str, str], Job] = {}
        for job in matrix.jobs:
            templates.setdefault((job.agent, job.model), job)
        while not aborted:
            pending = [
                group
                for group in unstable_groups(results)
                if sum(1 for result in results if result_group(result) == group) < matrix.max_repetitions
            ]
            if not pending:
                break
            for group in pending:
                case_id, agent, model = group
                done = sum(1 for result in results if result_group(result) == group)
                print(
                    f"repetitions disagreed for {case_id} | {agent} {model} after {done}; running another",
                    file=sys.stderr,
                )
                results.append(
                    execute(
                        cases_by_id[case_id],
                        dataclasses.replace(templates[(agent, model)], repetition=done + 1),
                    )
                )
        summary = {
            "schema_version": 1,
            "kind": "install-matrix",
            "executor": "docker",
            "status": "passed" if all(result["status"] == "passed" for result in results) else "failed",
            "total": len(results),
            "passed": sum(result["status"] == "passed" for result in results),
            "failed": sum(result["status"] == "failed" for result in results),
            "errors": sum(result["status"] == "error" for result in results),
            "aborted": aborted,
            "unstable": [
                {"case": case_id, "agent": agent, "model": model}
                for case_id, agent, model in unstable_groups(results)
            ],
            "results": [{"id": result["id"], "status": result["status"]} for result in results],
        }
        (output_root / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if summary["status"] == "passed" else 1


def build_image(args: argparse.Namespace) -> int:
    docker = shutil.which(args.docker)
    if docker is None:
        raise RuntimeError(f"Docker CLI not found: {args.docker}")
    command = [
        docker,
        "build",
        "--file",
        str(DEFAULT_DOCKERFILE),
        "--tag",
        args.image,
    ]
    for name, value in (
        ("CODEX_CLI_VERSION", args.codex_version),
        ("CLAUDE_CODE_VERSION", args.claude_version),
        ("OPENCODE_CLI_VERSION", args.opencode_version),
    ):
        if value:
            command.extend(["--build-arg", f"{name}={value}"])
    command.append(str(REPOSITORY_ROOT))
    return subprocess.run(
        command,
        check=False,
        env=docker_subprocess_environment(docker),
    ).returncode


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Agentic HIL LLM installation evaluation")
    subparsers = root.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="build pinned agent CLI image")
    build.add_argument("--docker", default="docker")
    build.add_argument("--image", default="agentic-hil-install-eval:local")
    build.add_argument("--codex-version")
    build.add_argument("--claude-version")
    build.add_argument("--opencode-version")
    build.set_defaults(function=build_image)

    run_parser = subparsers.add_parser("run", help="run agent CLI x model x case matrix")
    run_parser.add_argument("--docker", default="docker")
    run_parser.add_argument("--matrix", default=str(REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json"))
    run_parser.add_argument("--image")
    run_parser.add_argument("--source-root", default=str(REPOSITORY_ROOT))
    run_parser.add_argument("--output", required=True)
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument(
        "--refresh-login",
        action="store_true",
        help="write a token the agent CLI refreshed back over the stored login it came from",
    )
    run_parser.set_defaults(function=run_matrix)

    report_parser = subparsers.add_parser("report", help="summarise a finished evaluation output directory")
    report_parser.add_argument("--output", required=True)
    report_parser.set_defaults(function=report_results)

    routing_parser = subparsers.add_parser(
        "routing",
        help="show how each run answered the firmware question: MCP server, CLI, or neither",
    )
    routing_parser.add_argument("--output", required=True)
    routing_parser.set_defaults(function=routing_results)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.function(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
