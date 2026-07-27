from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from evals.install.config import load_matrix
from evals.install.runner import expected_mcp_tools, job_payload
from evals.install.verifier import target_mcp_tools

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


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
    contract = tmp_path / "harness" / "guest" / "tools.list.expected"
    contract.parent.mkdir(parents=True)
    contract.write_text("z_tool\na_tool\nz_tool\n", encoding="utf-8")

    with pytest.raises(ValueError, match="sorted and unique"):
        expected_mcp_tools(tmp_path)


def test_eval_image_does_not_embed_current_checkout_tool_contract() -> None:
    dockerfile = (REPOSITORY_ROOT / "evals" / "install" / "container" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "COPY harness/guest/tools.list.expected" not in dockerfile


def test_eval_image_allocates_an_unused_non_root_uid() -> None:
    dockerfile = (REPOSITORY_ROOT / "evals" / "install" / "container" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "useradd --create-home --user-group --shell /bin/bash eval" in dockerfile
    assert "--uid 1000" not in dockerfile
    assert "\nUSER eval\n" in dockerfile
