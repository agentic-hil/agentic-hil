from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from check_shipped_references import (  # noqa: E402
    index_problems,
    main,
    package_version,
    shipped_documents,
    violations,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_the_gate_runs_on_every_python_this_project_supports() -> None:
    """tomllib is stdlib only from 3.11 and this project still supports 3.10.

    Importing it made the gate itself the thing that failed, on a matrix leg the
    developer machine never runs.
    """
    source = (REPOSITORY_ROOT / "tools" / "check_shipped_references.py").read_text(encoding="utf-8")
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # The module, not the names it brings in: "from pathlib import Path"
            # imports pathlib, and asserting on Path would prove nothing.
            modules.add(node.module.split(".")[0])

    assert "tomllib" not in modules
    # check_version_consistency is the other half of the same gate and imports
    # nothing outside the standard library either. It is here because it owns
    # the one question this file must not answer twice: which version a pin
    # names when the tree it is read from carries a development suffix.
    assert modules <= {"__future__", "check_version_consistency", "re", "sys", "pathlib"}, sorted(modules)


def test_the_repository_ships_no_reference_a_reader_must_not_copy() -> None:
    assert main([str(REPOSITORY_ROOT)]) == 0


def test_a_pin_is_compared_against_the_release_and_never_against_this_tree(tmp_path: Path) -> None:
    """Between releases the tree is ahead of everything a reader can install.

    The tag pin in TROUBLESHOOTING.md is the documented route while PyPI is
    unreachable, so it names the release. Comparing it against the development
    version this tree builds would call the one correct pin in the repository
    wrong, on every commit of every cycle.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "agentic-hil"\nversion = "1.2.4.dev0"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text("## [1.2.3] - 2020-01-01\n\nThe release.\n", encoding="utf-8")

    version = package_version(tmp_path)

    assert version == "1.2.3"
    assert violations(Path("doc.md"), 'uv tool install "git+https://github.com/agentic-hil/agentic-hil@v1.2.3"', version) == []


def test_every_document_a_reader_receives_is_covered() -> None:
    covered = {path.relative_to(REPOSITORY_ROOT).as_posix() for path in shipped_documents(REPOSITORY_ROOT)}

    # The guide is delivered as a single raw file and the skills are installed
    # into agent homes; both reach a reader who cannot check anything.
    assert "AI_AGENT_QUICKSTART.md" in covered
    assert "src/agentic_hil/skills/agentic-hil/SKILL.md" in covered
    assert "plugins/agentic-hil/skills/agentic-hil/SKILL.md" in covered
    assert "docs/mcp-hosts.md" in covered
    # llms.txt is served from the short host and fetched on its own, so it
    # reaches a reader with no repository around them either.
    assert "llms.txt" in covered


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


def test_the_index_this_repository_serves_links_only_files_that_are_here() -> None:
    """The whole of llms.txt is its links, so a link that rots takes the file with it.

    The first llms.txt was deleted because nothing served it and its onward
    references were bare filenames an external fetcher could not resolve. This
    one is served beside the installers, which is what makes the same rot a
    reader's problem rather than a dead file's.
    """
    assert index_problems(REPOSITORY_ROOT) == []


def _index(tmp_path: Path, body: str) -> Path:
    (tmp_path / "llms.txt").write_text(body, encoding="utf-8")
    return tmp_path


def test_a_relative_link_in_the_index_is_refused(tmp_path: Path) -> None:
    """The failure that killed the first one: a filename with no host in front of it."""
    root = _index(tmp_path, "- [Installation](docs/installation.md): the install paths.\n")

    found = index_problems(root)

    assert found, "a bare filename resolves to nothing for an external fetcher"
    assert "resolves only inside a checkout" in found[0]


def test_a_link_to_a_path_this_repository_no_longer_holds_is_refused(tmp_path: Path) -> None:
    root = _index(
        tmp_path,
        "- [Tools](https://github.com/agentic-hil/agentic-hil/blob/master/docs/moved-away.md): the tools.\n",
    )

    found = index_problems(root)

    assert found
    assert "docs/moved-away.md is not in this repository" in found[0]


def test_a_link_that_leaves_the_repository_is_read_and_not_followed(tmp_path: Path) -> None:
    """Another repository's paths are not this gate's to assert on, only its form is."""
    root = _index(
        tmp_path,
        "- [STM32 starter](https://github.com/agentic-hil/stm32-starter): three steps on a board.\n"
        "- [Read the Docs](https://agentic-hil.readthedocs.io/): the rendered documentation.\n",
    )

    assert index_problems(root) == []


def test_a_link_that_climbs_out_of_the_repository_is_refused(tmp_path: Path) -> None:
    root = _index(
        tmp_path,
        "- [Elsewhere](https://github.com/agentic-hil/agentic-hil/blob/master/../secrets.md): no.\n",
    )

    found = index_problems(root)

    assert found
    assert "not a path in this repository" in found[0]


def test_a_tree_with_no_index_is_not_a_failure(tmp_path: Path) -> None:
    """The gate runs against sdists and checkouts that carry no llms.txt."""
    assert index_problems(tmp_path) == []
