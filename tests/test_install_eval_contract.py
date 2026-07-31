from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from evals.install.config import load_matrix
from evals.install.runner import expected_mcp_tools, job_payload
from evals.install.verifier import target_mcp_tools

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTAINER_ENTRYPOINT = REPOSITORY_ROOT / "evals" / "install" / "container_entrypoint.py"


def _job_key_accesses(source: str) -> tuple[set[str], set[str]]:
    """Collect the ``job[...]`` and ``job["case"][...]`` keys a module reads."""
    top_level: set[str] = set()
    case: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Constant):
            continue
        key = node.slice.value
        if not isinstance(key, str):
            continue
        container = node.value
        if isinstance(container, ast.Name) and container.id == "job":
            top_level.add(key)
        elif (
            isinstance(container, ast.Subscript)
            and isinstance(container.value, ast.Name)
            and container.value.id == "job"
            and isinstance(container.slice, ast.Constant)
            and container.slice.value == "case"
        ):
            case.add(key)
    return top_level, case


def test_container_entrypoint_reads_only_keys_the_runner_writes() -> None:
    matrix = load_matrix(REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json")
    payload = job_payload(
        matrix,
        matrix.cases[0],
        matrix.jobs[0],
        REPOSITORY_ROOT,
        REPOSITORY_ROOT,
    )
    top_level, case = _job_key_accesses(CONTAINER_ENTRYPOINT.read_text(encoding="utf-8"))

    assert case, "the entrypoint must read the case description"
    assert "prompt_template" in case, "the prompt key must stay bound to the runner payload"
    assert top_level <= set(payload), sorted(top_level - set(payload))
    assert case <= set(payload["case"]), sorted(case - set(payload["case"]))
    # Pinned in both directions: the runner must write the effort and the
    # entrypoint must read it, or a level is requested and never applied.
    assert "reasoning_effort" in payload
    assert "reasoning_effort" in top_level


def test_every_agent_session_receives_the_reasoning_effort() -> None:
    """The measured session is the one the routing report reads.

    An effort that reached only the installing session would leave the
    follow-up answering at the CLI's default while the result claims the level
    that was asked for.
    """
    tree = ast.parse(CONTAINER_ENTRYPOINT.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "build_agent_command"
    ]

    assert len(calls) == 3, "one call per agent session: install, confirmation, follow-up"
    for call in calls:
        assert any(keyword.arg == "reasoning_effort" for keyword in call.keywords)


def test_job_binds_mcp_contract_from_target_source() -> None:
    matrix = load_matrix(REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json")
    names = expected_mcp_tools(REPOSITORY_ROOT)

    payload = job_payload(
        matrix,
        matrix.cases[0],
        matrix.jobs[0],
        REPOSITORY_ROOT,
        REPOSITORY_ROOT,
    )

    assert payload["target"]["expected_mcp_tools"] == names
    assert target_mcp_tools(payload["target"]) == set(names)


def test_target_mcp_contract_rejects_digest_mismatch() -> None:
    names = ["one_tool", "two_tools"]
    target = {
        "expected_mcp_tools": names,
        "expected_mcp_tools_digest": hashlib.sha256(b"different\n").hexdigest(),
    }

    with pytest.raises(ValueError, match="digest mismatch"):
        target_mcp_tools(target)


def test_source_mcp_contract_must_be_sorted_and_unique(tmp_path: Path) -> None:
    contract = tmp_path / "evals" / "install" / "tools.list.expected"
    contract.parent.mkdir(parents=True)
    contract.write_text("z_tool\na_tool\nz_tool\n", encoding="utf-8")

    with pytest.raises(ValueError, match="sorted and unique"):
        expected_mcp_tools(tmp_path)


def test_eval_image_does_not_embed_current_checkout_tool_contract() -> None:
    dockerfile = (REPOSITORY_ROOT / "evals" / "install" / "container" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "COPY evals/install/tools.list.expected" not in dockerfile


def test_eval_image_allocates_an_unused_non_root_uid() -> None:
    dockerfile = (REPOSITORY_ROOT / "evals" / "install" / "container" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "useradd --create-home --user-group --shell /bin/bash eval" in dockerfile
    assert "--uid 1000" not in dockerfile
    assert "\nUSER eval\n" in dockerfile
