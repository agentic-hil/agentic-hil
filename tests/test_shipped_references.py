from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from check_shipped_references import main, shipped_documents, violations  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_the_repository_ships_no_reference_a_reader_must_not_copy() -> None:
    assert main([str(REPOSITORY_ROOT)]) == 0


def test_every_document_a_reader_receives_is_covered() -> None:
    covered = {path.relative_to(REPOSITORY_ROOT).as_posix() for path in shipped_documents(REPOSITORY_ROOT)}

    # The guide is delivered as a single raw file and the skills are installed
    # into agent homes; both reach a reader who cannot check anything.
    assert "AI_AGENT_QUICKSTART.md" in covered
    assert "src/agentic_hil/skills/agentic-hil/SKILL.md" in covered
    assert "plugins/agentic-hil/skills/agentic-hil/SKILL.md" in covered
    assert "docs/mcp-hosts.md" in covered


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('uv tool install "git+https://github.com/agentic-hil/agentic-hil@feature/x"', "mutable"),
        ('uv tool install "git+https://github.com/agentic-hil/agentic-hil.git@main"', "mutable"),
        ("uvx --from git+https://github.com/agentic-hil/agentic-hil agentic-hil --version", "without a pinned"),
        ('uv tool install "git+https://github.com/agentic-hil/agentic-hil@v0.1.0"', "but this release is"),
    ],
)
def test_a_reference_a_reader_must_not_copy_is_refused(line: str, expected: str) -> None:
    found = violations(Path("doc.md"), line, "9.9.9")

    assert found, line
    assert expected in found[0]


@pytest.mark.parametrize(
    "line",
    [
        'uv tool install "git+https://github.com/agentic-hil/agentic-hil@v9.9.9"',
        "See https://github.com/agentic-hil/agentic-hil for the source.",
        "/plugin install agentic-hil@agentic-hil",
        'uv tool install --upgrade "agentic-hil>=0.4.0"',
    ],
)
def test_a_legitimate_reference_is_accepted(line: str) -> None:
    assert violations(Path("doc.md"), line, "9.9.9") == []
