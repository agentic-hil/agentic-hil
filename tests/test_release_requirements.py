"""The release workflow installs nothing it cannot verify by hash.

The verification job that runs after the PyPI upload installs the published
distribution back from the index. It used to do so by version alone, which is
the one unhashed ``pip install`` the release pipeline carried: whatever the
index served under that version was installed and asked to answer for itself.
Now the wheel is installed by the hash of the file the same run built, with
dependency resolution off, and its runtime dependencies come from
``requirements/runtime.txt``, locked with hashes like the build tooling in
``requirements/build.txt``.

That lock is only as true as its input. These tests hold the input to the
dependencies ``pyproject.toml`` declares, so a dependency added to the package
without regenerating the lock fails here instead of in the release run, and
hold the workflow to ``--require-hashes`` on every install it performs.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_IN = REPOSITORY_ROOT / "requirements" / "runtime.in"
RUNTIME_TXT = REPOSITORY_ROOT / "requirements" / "runtime.txt"
RELEASE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "workflow.yml"

pytestmark = pytest.mark.skipif(
    not RUNTIME_IN.exists() or not RELEASE_WORKFLOW.exists(),
    reason="the release pipeline's inputs are repository content, not package content",
)


def _requirement_lines(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def _normalised(requirement: str) -> str:
    # Whitespace and the quoting of a marker carry no meaning to pip; the
    # comparison is on what the requirement says.
    return re.sub(r"\s+", "", requirement).replace("'", '"').lower()


def test_the_runtime_lock_input_is_exactly_what_pyproject_declares() -> None:
    declared = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["dependencies"]
    assert sorted(_normalised(line) for line in _requirement_lines(RUNTIME_IN)) == sorted(
        _normalised(dependency) for dependency in declared
    )


def test_every_pin_in_the_runtime_lock_carries_a_hash() -> None:
    text = RUNTIME_TXT.read_text(encoding="utf-8")
    pins = re.findall(r"^([A-Za-z0-9_.\-]+)==", text, flags=re.MULTILINE)
    assert pins, "the runtime lock pins nothing"
    # Each pin is followed by its hash lines before the next pin or the "via"
    # comment that ends its block.
    blocks = re.split(r"^(?=[A-Za-z0-9_.\-]+==)", text, flags=re.MULTILINE)[1:]
    unhashed = [block.splitlines()[0] for block in blocks if "--hash=sha256:" not in block]
    assert unhashed == []
    assert "uv pip compile --generate-hashes" in text


def test_the_release_workflow_installs_only_with_require_hashes() -> None:
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    installs = [line.strip() for line in text.splitlines() if re.search(r"\bpip install\b", line)]
    assert installs, "the release workflow performs no pip install"
    # A file this run built and installs from its own disk carries no index
    # lookup to pin; everything fetched from an index is hashed.
    from_index = [line for line in installs if not re.search(r"\bpip install\b.*\bdist/", line)]
    assert from_index, "the release workflow installs nothing from an index"
    unhashed = [line for line in from_index if "--require-hashes" not in line]
    assert unhashed == [], unhashed
    # The wheel itself is installed by the hash of the file the run built, not
    # by version alone, and without dependency resolution.
    assert "--no-deps --require-hashes -r verify-pin.txt" in text
    assert "pip hash --algorithm sha256" in text
    assert "--require-hashes -r requirements/runtime.txt" in text
