"""Refuse a repository reference that a reader must not copy.

Testing an installation flow means pinning documents at the branch under test.
Such a pin installs unreviewed code for everyone who copies the command, and an
unpinned source install is no better: it resolves to whatever the default branch
holds at that moment. A tag pin that is not this release's is worse still,
because the tag pin is the documented route for the case where PyPI is
unreachable, so a wrong one closes the exit during the outage it covers.

This used to be a checklist in a file somebody had to remember to read before
merging. It runs in CI on every push and pull request, and again at release.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

IMMUTABLE_REFERENCE = re.compile(r"^(v\d+\.\d+\.\d+|[0-9a-f]{40})$")
# Anchored at the repository URL: "agentic-hil@agentic-hil" is the
# plugin@marketplace syntax Claude Code uses, not a git reference. The reference
# group is optional so that a missing pin is caught as well as a mutable one.
REPOSITORY_REFERENCE = re.compile(
    r"(?:git\+)?https://github\.com/agentic-hil/agentic-hil(?:\.git)?(@[^\s\"'`)]+)?"
)
INSTALLS_FROM_SOURCE = re.compile(r"git\+https://github\.com/agentic-hil/agentic-hil|--from\s+git\+")


def package_version(root: Path) -> str:
    """Read __version__ rather than pyproject.

    tomllib is stdlib only from 3.11 and this project still supports 3.10, so a
    TOML parse would make the gate itself the thing that fails. The release job
    already refuses a build where the two disagree.
    """
    tree = ast.parse((root / "src" / "agentic_hil" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            return str(ast.literal_eval(node.value))
    raise SystemExit("src/agentic_hil/__init__.py declares no __version__")


def shipped_documents(root: Path) -> list[Path]:
    """Every document a reader receives, in the distribution or on the page."""
    named = [
        root / "AI_AGENT_QUICKSTART.md",
        root / "README.md",
        root / "TROUBLESHOOTING.md",
        root / "AGENTS.md",
        root / "SECURITY.md",
        root / "server.json",
    ]
    globbed = [
        *sorted((root / "docs").rglob("*.md")),
        *sorted((root / "src" / "agentic_hil" / "skills").rglob("*.md")),
        *sorted((root / "plugins").rglob("*.md")),
        *sorted((root / "plugins").rglob("*.json")),
        *sorted((root / ".claude-plugin").rglob("*.json")),
        *sorted((root / "examples").rglob("*.md")),
    ]
    return [path for path in [*named, *globbed] if path.is_file()]


def violations(document: Path, text: str, version: str) -> list[str]:
    found = []
    for line in text.splitlines():
        for match in REPOSITORY_REFERENCE.finditer(line):
            reference = match.group(1)
            if reference is None:
                if INSTALLS_FROM_SOURCE.search(line):
                    found.append(f"{document} installs from the repository without a pinned reference: {line.strip()}")
                continue
            pinned = reference[1:]
            if not IMMUTABLE_REFERENCE.match(pinned):
                found.append(f"{document} pins a mutable reference: agentic-hil@{pinned}")
            elif pinned.startswith("v") and pinned != f"v{version}":
                found.append(f"{document} pins {pinned}, but this release is v{version}")
    return found


def main(argv: list[str] | None = None) -> int:
    root = Path(argv[0]) if argv else Path(__file__).resolve().parents[1]
    version = package_version(root)
    found = []
    for document in shipped_documents(root):
        found.extend(violations(document, document.read_text(encoding="utf-8"), version))
    for problem in found:
        print(problem, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
