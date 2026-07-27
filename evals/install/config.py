from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters import adapter_for

ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
OFFICIAL_GIT_SPEC = re.compile(
    r"^git\+https://github\.com/agentic-hil/agentic-hil(?:\.git)?@(?P<commit>[0-9a-fA-F]{40})$"
)
OFFICIAL_GUIDE_URL = "https://raw.githubusercontent.com/agentic-hil/agentic-hil/{commit}/AI_AGENT_QUICKSTART.md"
RESERVED_ENVIRONMENT = {
    "AGENTIC_HIL_CONFIG",
    "CODEX_HOME",
    "HOME",
    "PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "USERPROFILE",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
}
CREDENTIAL_FILE_KINDS = {
    "codex": {"codex-auth"},
    "claude-code": {"claude-auth"},
    "opencode": {"opencode-auth"},
}


@dataclass(frozen=True)
class Target:
    mode: str
    expected_version: str
    install_spec: str | None = None
    guide_url: str | None = None
    expected_commit: str | None = None


@dataclass(frozen=True)
class Case:
    id: str
    prompt_template: str
    fixture: str
    expected_outcome: str
    # Whether the case additionally requires evidence that a hardware request
    # was answered through an Agentic HIL tool rather than a raw command.
    requires_tool_use: bool = False


@dataclass(frozen=True)
class CredentialFile:
    kind: str
    path_environment: str


@dataclass(frozen=True)
class Job:
    agent: str
    model: str
    repetition: int
    timeout_seconds: int
    credentials: tuple[str, ...]
    credential_files: tuple[CredentialFile, ...]


MINIMUM_REPETITIONS = 2
MAXIMUM_REPETITIONS = 20


@dataclass(frozen=True)
class Matrix:
    target: Target
    cases: tuple[Case, ...]
    jobs: tuple[Job, ...]
    image: str
    max_repetitions: int = MINIMUM_REPETITIONS


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _positive_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")
    return value


def _repetitions(value: Any, label: str) -> int:
    """A single run cannot separate a stable result from a lucky one."""
    count = _positive_integer(value, label)
    if count < MINIMUM_REPETITIONS:
        raise ValueError(f"{label} must be at least {MINIMUM_REPETITIONS}")
    if count > MAXIMUM_REPETITIONS:
        raise ValueError(f"{label} must not exceed {MAXIMUM_REPETITIONS}")
    return count


def load_case(path: Path) -> Case:
    # Windows editors and PowerShell write UTF-8 with a byte order mark.
    raw = _object(json.loads(path.read_text(encoding="utf-8-sig")), "case")
    if raw.get("schema_version") != 1:
        raise ValueError("case.schema_version must be 1")
    return Case(
        id=_string(raw.get("id"), "case.id"),
        prompt_template=_string(raw.get("prompt"), "case.prompt"),
        fixture=_string(raw.get("fixture", "clean"), "case.fixture"),
        expected_outcome=_string(raw.get("expected_outcome", "success"), "case.expected_outcome"),
        requires_tool_use=_boolean(raw.get("requires_tool_use", False), "case.requires_tool_use"),
    )


def _load_target(raw: dict[str, Any]) -> Target:
    mode = _string(raw.get("mode"), "target.mode")
    expected_version = _string(raw.get("expected_version"), "target.expected_version")
    if mode == "local":
        return Target(mode=mode, expected_version=expected_version)
    if mode != "remote":
        raise ValueError("target.mode must be 'local' or 'remote'")

    install_spec = _string(raw.get("install_spec"), "target.install_spec")
    match = OFFICIAL_GIT_SPEC.fullmatch(install_spec)
    if match is None:
        raise ValueError("remote target.install_spec must use the official repository at a full 40-character commit")
    expected_commit = match.group("commit").lower()
    configured_commit = raw.get("expected_commit")
    if (
        configured_commit is not None
        and _string(configured_commit, "target.expected_commit").lower() != expected_commit
    ):
        raise ValueError("target.expected_commit does not match target.install_spec")
    guide_url = OFFICIAL_GUIDE_URL.format(commit=expected_commit)
    configured_guide_url = raw.get("guide_url")
    if configured_guide_url is not None and _string(configured_guide_url, "target.guide_url") != guide_url:
        raise ValueError("target.guide_url must be the immutable official raw guide URL for target.install_spec")
    return Target(
        mode=mode,
        expected_version=expected_version,
        install_spec=install_spec,
        guide_url=guide_url,
        expected_commit=expected_commit,
    )


def _credentials(raw: Any, label: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list")
    values: list[str] = []
    for index, value in enumerate(raw):
        name = _string(value, f"{label}[{index}]")
        if ENVIRONMENT_NAME.fullmatch(name) is None:
            raise ValueError(f"{label}[{index}] is not a valid environment variable name")
        if name in RESERVED_ENVIRONMENT:
            raise ValueError(f"{label}[{index}] may not override eval isolation")
        if name not in values:
            values.append(name)
    return tuple(values)


def _credential_files(raw: Any, label: str, agent: str) -> tuple[CredentialFile, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list")
    values: list[CredentialFile] = []
    seen_kinds: set[str] = set()
    for index, value in enumerate(raw):
        item = _object(value, f"{label}[{index}]")
        kind = _string(item.get("kind"), f"{label}[{index}].kind")
        if kind not in CREDENTIAL_FILE_KINDS[agent]:
            raise ValueError(f"{label}[{index}].kind is unsupported for {agent}: {kind!r}")
        path_environment = _string(
            item.get("path_environment"),
            f"{label}[{index}].path_environment",
        )
        if ENVIRONMENT_NAME.fullmatch(path_environment) is None:
            raise ValueError(f"{label}[{index}].path_environment is not a valid environment variable name")
        if path_environment in RESERVED_ENVIRONMENT:
            raise ValueError(f"{label}[{index}].path_environment may not override eval isolation")
        if kind in seen_kinds:
            raise ValueError(f"{label} contains duplicate kind {kind!r}")
        seen_kinds.add(kind)
        values.append(CredentialFile(kind=kind, path_environment=path_environment))
    return tuple(values)


def load_matrix(path: str | Path) -> Matrix:
    matrix_path = Path(path).resolve()
    raw = _object(json.loads(matrix_path.read_text(encoding="utf-8-sig")), "matrix")
    if raw.get("schema_version") != 1:
        raise ValueError("matrix.schema_version must be 1")

    target = _load_target(_object(raw.get("target"), "target"))
    raw_cases = raw.get("cases")
    if raw_cases is None and "case" in raw:
        raw_cases = [raw["case"]]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("cases must be a non-empty list")
    cases = tuple(
        load_case((matrix_path.parent / _string(value, f"cases[{index}]")).resolve())
        for index, value in enumerate(raw_cases)
    )
    allowed_fixtures = {"clean", "preserve-user-config", "unsafe-existing-config"}
    allowed_outcomes = {"success", "safe-failure"}
    for case in cases:
        if case.fixture not in allowed_fixtures:
            raise ValueError(f"case {case.id!r} has unsupported fixture {case.fixture!r}")
        if case.expected_outcome not in allowed_outcomes:
            raise ValueError(f"case {case.id!r} has unsupported expected_outcome {case.expected_outcome!r}")
    case_ids = [case.id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("case ids must be unique")
    image = _string(raw.get("image", "agentic-hil-install-eval:local"), "image")
    default_repetitions = _repetitions(raw.get("repetitions", MINIMUM_REPETITIONS), "repetitions")
    default_timeout = _positive_integer(raw.get("timeout_seconds", 1800), "timeout_seconds")
    if "pass_environment" in raw:
        raise ValueError("matrix.pass_environment is unsupported; declare jobs[].credentials explicitly")

    raw_jobs = raw.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise ValueError("jobs must be a non-empty list")

    jobs: list[Job] = []
    for index, value in enumerate(raw_jobs):
        item = _object(value, f"jobs[{index}]")
        adapter = adapter_for(_string(item.get("agent"), f"jobs[{index}].agent"))
        model = _string(item.get("model"), f"jobs[{index}].model")
        repetitions = _repetitions(item.get("repetitions", default_repetitions), f"jobs[{index}].repetitions")
        timeout = _positive_integer(item.get("timeout_seconds", default_timeout), f"jobs[{index}].timeout_seconds")
        if "pass_environment" in item:
            raise ValueError(f"jobs[{index}].pass_environment is unsupported; use jobs[{index}].credentials")
        credentials = _credentials(item.get("credentials"), f"jobs[{index}].credentials")
        credential_files = _credential_files(
            item.get("credential_files"),
            f"jobs[{index}].credential_files",
            adapter.id,
        )
        if not credentials and not credential_files:
            raise ValueError(f"jobs[{index}] must declare credentials and/or credential_files")
        if adapter.id != "opencode":
            unsupported = sorted(set(credentials) - set(adapter.auth_environment))
            if unsupported:
                raise ValueError(
                    f"jobs[{index}].credentials contains unsupported names for {adapter.id}: {unsupported}"
                )
        jobs.extend(
            Job(
                agent=adapter.id,
                model=model,
                repetition=repetition,
                timeout_seconds=timeout,
                credentials=credentials,
                credential_files=credential_files,
            )
            for repetition in range(1, repetitions + 1)
        )

    job_ids = [(job.agent, job.model, job.repetition) for job in jobs]
    if len(job_ids) != len(set(job_ids)):
        raise ValueError("agent, model, and repetition combinations must be unique")

    planned = max(job.repetition for job in jobs)
    max_repetitions = _repetitions(
        raw.get("max_repetitions", max(MINIMUM_REPETITIONS + 3, planned)),
        "max_repetitions",
    )
    if max_repetitions < planned:
        raise ValueError(f"max_repetitions must not be below the planned {planned} repetitions")
    return Matrix(
        target=target,
        cases=cases,
        jobs=tuple(jobs),
        image=image,
        max_repetitions=max_repetitions,
    )
