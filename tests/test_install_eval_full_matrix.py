from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FULL_MATRIX = REPOSITORY_ROOT / "evals" / "install" / "matrix.full.json"

EXPECTED_CASES = [
    "cases/quickstart.json",
    "cases/preserve-user-config.json",
    "cases/unsafe-existing-config.json",
    "cases/firmware-routing.json",
    "cases/firmware-routing-without-skill.json",
    "cases/firmware-readiness.json",
    "cases/firmware-readiness-without-skill.json",
    "cases/firmware-flash-request.json",
    "cases/firmware-flash-request-without-skill.json",
]
EXPECTED_MODELS = {
    "codex": {
        "gpt-5.6-sol",
        "gpt-5.4",
        "gpt-5.4-mini",
    },
    "claude-code": {
        "claude-sonnet-4-6",
        "claude-opus-4-8",
        "claude-haiku-4-5",
    },
    "opencode": {
        "openai/gpt-5.6-sol",
        "openai/gpt-5.4",
        "openai/gpt-5.4-mini",
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-opus-4-8",
        "anthropic/claude-haiku-4-5",
    },
}


def test_full_matrix_covers_every_selected_case_agent_and_model() -> None:
    document = json.loads(FULL_MATRIX.read_text(encoding="utf-8"))
    models: dict[str, set[str]] = defaultdict(set)

    for job in document["jobs"]:
        models[job["agent"]].add(job["model"])
        expected_credential = (
            "ANTHROPIC_API_KEY"
            if job["model"].startswith(("claude-", "anthropic/"))
            else "OPENAI_API_KEY"
        )
        assert job["credentials"] == [expected_credential]

    assert document["schema_version"] == 1
    assert document["cases"] == EXPECTED_CASES
    assert dict(models) == EXPECTED_MODELS
    assert len(document["jobs"]) == 12
    assert document["repetitions"] == 2
    assert document["max_repetitions"] == 5

    initial_runs = (
        len(document["cases"])
        * len(document["jobs"])
        * document["repetitions"]
    )
    assert initial_runs == 216
