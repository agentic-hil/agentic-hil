from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import fields
from pathlib import Path

import pytest

from agentic_hil.config import GENERATED_PROJECT_PERMISSIONS
from agentic_hil.knowledge import EXCLUSIVE_FLASH_PERMISSIONS
from agentic_hil.types import (
    CURRENT_CONFIG_VERSION,
    LEGACY_CONFIG_VERSION,
    READ_FREE_CONFIG_VERSION,
    SUPPORTED_CONFIG_VERSIONS,
    ProjectPermissions,
)
from evals.install import verifier
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


def test_the_operator_answers_generically_rather_than_by_model() -> None:
    """Any agent that stops to ask gets the same answer, in the operator's voice.

    Measured across models: one asked for the guide it had never seen, another
    about login files it found under /run, another only about the scope. A reply
    written for one model's wording answers the next model not at all.
    """
    text = CONTAINER_ENTRYPOINT.read_text(encoding="utf-8")
    reply = re.search(r"OPERATOR_REPLY = \((.*?)\n\)", text, re.DOTALL)

    assert reply is not None
    body = reply.group(1)
    for named in ("sonnet", "claude", "codex", "opencode", "gpt"):
        assert named not in body.lower(), f"the operator's answer names {named}"
    for covered in ("{guide}", "user-level", "/run", "scope"):
        assert covered in body, f"the operator's answer does not address {covered}"
    assert re.search(r"for attempt in range\(1, MAX_OPERATOR_REPLIES \+ 1\)", text), "the reply is sent once only"


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


def test_the_verifier_permission_whitelist_is_the_one_this_release_generates() -> None:
    """The drift that stranded a full matrix, caught in the pull request instead.

    The verifier spells this list out rather than importing it, on purpose: it
    checks an installed distribution and must not read its expectations out of
    the thing it is verifying, or a release that both added a permission and
    wrote it would move the expectation along with itself. The price of spelling
    it out is that `allow_recover` shipped and every job in a full matrix failed
    on a config the install was right to write. This is where that price is
    paid instead: one release ahead of the eval, in the change that adds the
    permission, where the fix is one word.
    """
    assert verifier.PROJECT_PERMISSION_FLAGS == GENERATED_PROJECT_PERMISSIONS
    # Stated against the schema as well as against the generation: a permission
    # the type declares and no generation writes is still one this eval must
    # recognise in a file an operator edited.
    assert set(verifier.PROJECT_PERMISSION_FLAGS) == {field.name for field in fields(ProjectPermissions)}


def test_the_verifier_flash_interlock_pair_is_the_one_the_interlock_refuses_on() -> None:
    assert set(verifier.EXCLUSIVE_FLASH_PERMISSIONS) == set(EXCLUSIVE_FLASH_PERMISSIONS)


def test_the_verifier_reads_every_config_version_this_release_writes() -> None:
    """Version 3 was the second half of the same stranding, unread behind the first."""
    assert verifier.SUPPORTED_CONFIG_VERSIONS == SUPPORTED_CONFIG_VERSIONS
    assert verifier.LEGACY_CONFIG_VERSION == LEGACY_CONFIG_VERSION
    assert verifier.READ_FREE_CONFIG_VERSION == READ_FREE_CONFIG_VERSION
    # What an install writes today has to be inside the family the check treats
    # as read-free, or it demands the read permissions this model removed.
    assert CURRENT_CONFIG_VERSION >= verifier.READ_FREE_CONFIG_VERSION
    assert CURRENT_CONFIG_VERSION in verifier.SUPPORTED_CONFIG_VERSIONS


def test_the_wrong_workspace_probe_calls_a_tool_this_server_serves() -> None:
    """A renamed tool would leave the probe asking for nothing and reading a refusal into it."""
    contract = set(expected_mcp_tools(REPOSITORY_ROOT))

    assert verifier.UNPROVISIONED_PROBE_TOOL in contract
