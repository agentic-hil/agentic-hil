"""Refuse a tree whose release version does not agree with itself.

A version lives in more places than anyone remembers. The release checklist in
`docs/release-strategy.md` named seven positions; the release that exposed this
carried a version in twelve files. The two install-eval matrices were among the
five nobody had written down, and `validate_source_version` in
`evals/install/runner.py` raises before a run starts, so both matrices —
including the runner CLI's own default — aborted on every tree newer than the
0.4.0 they still pinned. That went unnoticed across two releases, because the
only thing that enforced version agreement ran on `release: published`: after
the point of no return, since a PyPI version cannot be re-uploaded.
Implements hardci-hq#74 and hardci-hq#75.

So this is one module rather than a step in a release job. It reads files and
compares strings — no secret, no OIDC, no tag — which is exactly the shape that
belongs on a pull request. `.github/workflows/ci.yml` runs it on every push and
pull request; `.github/workflows/workflow.yml` runs the same code again at
release with `--release-tag`, the one comparison that genuinely needs a tag.

`locations` below is the list. It is not a copy of the checklist: it is the
check, and `--list` prints it, so `docs/release-strategy.md` can point here
instead of repeating an enumeration that drifts. `uncovered_files` closes the
other half — any file carrying the release version that no entry accounts for
is an error, so a new home for the version cannot appear unnoticed. That sweep
skips covered files, so a mention shape an entry's extractor cannot see is a
mention nothing checks: the `agentic-hil@vX.Y.Z` git-tag pin in
TROUBLESHOOTING.md drifted by hand through four releases that way
(hardci-hq#86). Tag pins are now an entry of their own, and `unclaimed_pins`
refuses a covered file whose project-tied `@v` pins its entries do not state.

tomllib is deliberately absent: it is stdlib only from 3.11 and this project
still supports 3.10, so importing it would make the gate itself the thing that
fails on a matrix leg the developer machine never runs.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

RELEASE_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
# An optional leading "v" so a tag pin is seen, and no word character or dot on
# either side so "1.2.3" is not read out of "11.2.3" or "1.2.30".
SEMVER = re.compile(r"(?<![\w.])v?(\d+\.\d+\.\d+)(?![\w.])")
PYPROJECT_TABLE = re.compile(r"^\[(?P<table>[^\]]+)\]\s*$")
TOML_STRING = re.compile(r"""^(?P<key>[A-Za-z0-9_-]+)\s*=\s*["'](?P<value>[^"']*)["']\s*$""")
SKILL_METADATA_VERSION = re.compile(r"""^\s*agentic_hil_version:\s*["']([^"']+)["']\s*$""", re.MULTILINE)
CHANGELOG_RELEASE = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)
EXPECTED_VERSION_FIELD = re.compile(r"""["']expected_version["']\s*:\s*["']([^"']+)["']""")
# An `@vX.Y.Z` pin: a git tag in an install URL, an action pin, a tag in a
# release link. The reference the `@` pins — the token before it — decides
# whose version the pin states: only a reference that names this project
# (`agentic-hil` is the PyPI name, both halves of the repository path, and the
# organisation any action of ours lives under) states this project's version.
# `actions/checkout@v4` and every other third-party pin carries somebody
# else's version and must stay invisible to this gate.
TAG_PIN = re.compile(r"""([^\s"'`@]+)@v(\d+\.\d+\.\d+)(?![\w.])""")

# The sweep walks the working tree, so it meets whatever else lives there.
SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        ".testenv",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        "htmlcov",
        ".tox",
        # Hash-pinned third-party locks. A dependency that happens to sit at our
        # version number says nothing about our release, and nobody hand-edits
        # these to prepare one.
        "requirements",
    }
)
# Generated output and local agent state, by path rather than by name so that
# ".claude-plugin" — which is content — is not caught with ".claude".
# ".agentic-loop" holds review documents and agent transcripts from
# tools/agent_review_loop.py. A transcript quotes whatever the agents discussed,
# so one that mentions a version is reporting a conversation, not tracking the
# release -- and running the loop in this tree used to fail this gate.
SKIPPED_PATHS = frozenset({".agentic-hil", ".agentic-loop", ".claude", "evals/install/artifacts"})
SKIPPED_NAMES = frozenset({"package-lock.json", "uv.lock", "poetry.lock"})
SKIPPED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".ico", ".whl", ".gz", ".zip"})
MAX_SWEPT_BYTES = 4_000_000

# A file may carry the release version without tracking it, but it has to say so
# here, with the reason. An entry is a claim someone can argue with; a file
# quietly left out of the list is how this failed the first time.
UNTRACKED_MENTIONS: dict[str, str] = {
    # AI_AGENT_QUICKSTART.md pins ">=0.4.0" as the release that first shipped
    # `setup`. It is a capability floor and must not follow the release: raising
    # it would reject installations that are new enough.
    "AI_AGENT_QUICKSTART.md": "capability floor, deliberately not the current release",
    # These two name, in prose, the release a defect shipped in, so a reader can
    # tell which behaviour the test pins and why it exists. Naming that release
    # is the point; following the current one would erase the history the
    # comment records. Neither file pins a version anything installs against.
    "tests/fixtures/fake_openocd.py": "prose reference to the release a defect shipped in",
    "tests/test_agentic_hil.py": "prose reference to the release a defect shipped in",
}


@dataclass(frozen=True)
class Location:
    """One place that carries the release version."""

    path: str
    carries: str
    versions: tuple[str, ...] = ()


def _read(root: Path, relative: str) -> str:
    return (root / relative).read_text(encoding="utf-8")


def _project_table(root: Path) -> dict[str, str]:
    """The `[project]` table of pyproject.toml, as far as flat strings go.

    Enough for `name` and `version`, and it costs no TOML parser on 3.10.
    """
    table: dict[str, str] = {}
    inside = False
    for line in _read(root, "pyproject.toml").splitlines():
        header = PYPROJECT_TABLE.match(line)
        if header is not None:
            inside = header.group("table") == "project"
            continue
        if not inside:
            continue
        entry = TOML_STRING.match(line)
        if entry is not None:
            table[entry.group("key")] = entry.group("value")
    return table


def package_version(root: Path) -> str:
    version = _project_table(root).get("version")
    if version is None or not RELEASE_VERSION.match(version):
        raise SystemExit("pyproject.toml [project] declares no strict SemVer version")
    return version


def _init_version(root: Path) -> str:
    tree = ast.parse(_read(root, "src/agentic_hil/__init__.py"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            return str(ast.literal_eval(node.value))
    raise SystemExit("src/agentic_hil/__init__.py declares no __version__")


def _skill_metadata_version(root: Path, relative: str) -> str:
    match = SKILL_METADATA_VERSION.search(_read(root, relative))
    if match is None:
        raise SystemExit(f"{relative} has no agentic_hil_version metadata")
    return match.group(1)


def _skill_body_versions(root: Path, relative: str) -> tuple[str, ...]:
    """Every version the skill states in prose or in a command.

    A skill is installed into an agent's home and read by something that cannot
    check it, so the pinned install command and the version the agent is told to
    expect have to be this release. Exhaustive on purpose: the install command
    was pinned by a substring test that a second, stale mention slipped past.
    """
    body = SKILL_METADATA_VERSION.sub("", _read(root, relative))
    return tuple(match.group(1) for match in SEMVER.finditer(body))


def _requirement_pins(root: Path, relative: str) -> tuple[str, ...]:
    """Every `agentic-hil>=X.Y.Z` or `agentic-hil==X.Y.Z` in a document."""
    text = _read(root, relative)
    return tuple(re.findall(r"agentic-hil(?:>=|==)(\d+\.\d+\.\d+)", text))


def _tag_pins(root: Path, relative: str) -> tuple[str, ...]:
    """Every `@vX.Y.Z` pin in a document whose reference names this project.

    A different mention shape from a requirement pin: `>=` and `==` state a
    version, a tag pin states a git reference that happens to be one. The
    first release after this gate shipped proved the difference matters —
    `_requirement_pins` counted five occurrences in TROUBLESHOOTING.md and the
    sixth, `agentic-hil@v0.7.0` in a git URL, was invisible (hardci-hq#86).
    """
    text = _read(root, relative)
    return tuple(match.group(2) for match in TAG_PIN.finditer(text) if "agentic-hil" in match.group(1))


def _changelog_release(root: Path) -> str:
    match = CHANGELOG_RELEASE.search(_read(root, "CHANGELOG.md"))
    if match is None:
        raise SystemExit("CHANGELOG.md has no released semantic version heading")
    return match.group(1)


def _json_document(root: Path, relative: str) -> dict:
    return json.loads(_read(root, relative))


def locations(root: Path) -> list[Location]:
    """Every place the release version has to appear, and what carries it there.

    This is the list the release checklist used to keep by hand. Keeping it here
    means the documentation cannot fall behind the check, because it is the
    check.
    """
    registry = _json_document(root, "server.json")
    plugin = _json_document(root, "plugins/agentic-hil/.claude-plugin/plugin.json")
    marketplace = _json_document(root, ".claude-plugin/marketplace.json")
    example_matrix = _json_document(root, "evals/install/matrix.example.json")
    full_matrix = _json_document(root, "evals/install/matrix.full.json")

    return [
        Location("pyproject.toml", "[project] version: the distribution version", (package_version(root),)),
        Location("src/agentic_hil/__init__.py", "__version__: what the CLI and MCP server report", (_init_version(root),)),
        Location(
            "server.json",
            "version and packages[0].version: the MCP Registry entry",
            (str(registry["version"]), str(registry["packages"][0]["version"])),
        ),
        Location(
            "plugins/agentic-hil/.claude-plugin/plugin.json",
            "version: the Claude Code plugin manifest",
            (str(plugin["version"]),),
        ),
        Location(
            ".claude-plugin/marketplace.json",
            "plugins[0].version: the marketplace entry",
            (str(marketplace["plugins"][0]["version"]),),
        ),
        Location("CHANGELOG.md", "the newest ## [X.Y.Z] heading", (_changelog_release(root),)),
        Location(
            "src/agentic_hil/skills/agentic-hil/SKILL.md",
            "agentic_hil_version metadata, and every version stated in the body",
            (
                _skill_metadata_version(root, "src/agentic_hil/skills/agentic-hil/SKILL.md"),
                *_skill_body_versions(root, "src/agentic_hil/skills/agentic-hil/SKILL.md"),
            ),
        ),
        Location(
            "plugins/agentic-hil/skills/agentic-hil/SKILL.md",
            "agentic_hil_version metadata, the pinned install command, and every version stated in the body",
            (
                _skill_metadata_version(root, "plugins/agentic-hil/skills/agentic-hil/SKILL.md"),
                *_skill_body_versions(root, "plugins/agentic-hil/skills/agentic-hil/SKILL.md"),
            ),
        ),
        Location(
            "TROUBLESHOOTING.md",
            "every agentic-hil>= install pin the fix instructions hand a reader",
            _requirement_pins(root, "TROUBLESHOOTING.md"),
        ),
        # A second entry for the same file, because it is a different claim: a
        # git-tag pin, not a version requirement (hardci-hq#86).
        Location(
            "TROUBLESHOOTING.md",
            "the agentic-hil@vX.Y.Z git-tag pin: the documented install route while PyPI is unreachable",
            _tag_pins(root, "TROUBLESHOOTING.md"),
        ),
        Location(
            "evals/install/matrix.example.json",
            "target.expected_version: the install-eval runner refuses to start when it disagrees",
            (str(example_matrix["target"]["expected_version"]),),
        ),
        Location(
            "evals/install/matrix.full.json",
            "target.expected_version: the install-eval runner refuses to start when it disagrees",
            (str(full_matrix["target"]["expected_version"]),),
        ),
        Location(
            "evals/install/README.md",
            "expected_version in the documented matrix example a reader copies",
            tuple(EXPECTED_VERSION_FIELD.findall(_read(root, "evals/install/README.md"))),
        ),
    ]


def version_problems(root: Path, release_tag: str | None = None) -> list[str]:
    """Every version source that disagrees with pyproject.toml."""
    version = package_version(root)
    found = []
    for location in locations(root):
        if not location.versions:
            found.append(f"{location.path} carries no version, but {location.carries} was expected")
            continue
        for stated in location.versions:
            if stated != version:
                found.append(f"{location.path} states {stated}, but this release is {version} ({location.carries})")
    if release_tag:
        tagged = release_tag[1:] if release_tag.startswith("v") else release_tag
        if tagged != version:
            found.append(f"release tag {release_tag} does not match pyproject.toml {version}")
    return found


def _swept_files(root: Path) -> list[Path]:
    found: list[Path] = []

    def walk(directory: Path) -> None:
        for entry in sorted(directory.iterdir()):
            if entry.is_symlink():
                continue
            if entry.is_dir():
                relative = entry.relative_to(root).as_posix()
                if entry.name in SKIPPED_DIRECTORIES or entry.name.endswith(".egg-info"):
                    continue
                if relative in SKIPPED_PATHS:
                    continue
                walk(entry)
            elif entry.is_file():
                if entry.name in SKIPPED_NAMES or entry.suffix.lower() in SKIPPED_SUFFIXES:
                    continue
                if entry.stat().st_size > MAX_SWEPT_BYTES:
                    continue
                found.append(entry)

    walk(root)
    return found


def uncovered_files(root: Path, version: str | None = None) -> list[str]:
    """Files that carry the release version and no entry in `locations` claims.

    This is what makes the list underivable from memory: adding a thirteenth
    home for the version without adding it here turns the build red, and the
    message says where.
    """
    version = version or package_version(root)
    carries = re.compile(rf"(?<![\w.])v?{re.escape(version)}(?![\w.])")
    covered = {location.path for location in locations(root)}
    found = []
    for path in _swept_files(root):
        relative = path.relative_to(root).as_posix()
        if relative in covered or relative in UNTRACKED_MENTIONS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if carries.search(text):
            found.append(
                f"{relative} carries version {version} but no check covers it: "
                f"add it to locations() in tools/check_version_consistency.py, "
                f"or to UNTRACKED_MENTIONS with the reason it must not track the release"
            )
    return found


def unclaimed_pins(root: Path) -> list[str]:
    """Project-tied `@vX.Y.Z` pins in covered files that no entry states.

    `uncovered_files` deliberately skips a covered file, so a mention shape the
    file's extractors cannot see is checked by nothing — the gap hardci-hq#86
    found, where a file counted as covered while a pin in it drifted by hand
    through four releases. This is the closing half: every pin of this shape in
    a covered file must carry a version the file's own entries state, which
    `version_problems` then compares against the release. A pin equal to a
    stated version passes here and is caught there the moment it goes stale.

    Two gaps stay open, named rather than closed. A file in UNTRACKED_MENTIONS
    is excused wholesale by its declared reason. And an uncovered file is the
    sweep's job, which recognises the current release string — so a pin born
    already stale in a brand-new file is seen only by
    tools/check_shipped_references.py, and only as a full repository URL in a
    shipped document.
    """
    claimed: dict[str, set[str]] = {}
    for location in locations(root):
        claimed.setdefault(location.path, set()).update(location.versions)
    found = []
    for relative in sorted(claimed):
        for pinned in _tag_pins(root, relative):
            if pinned not in claimed[relative]:
                found.append(
                    f"{relative} pins this project at @v{pinned}, but no entry in locations() claims that pin: "
                    f"extend the file's entry in tools/check_version_consistency.py so the pin is checked"
                )
    return found


def contract_problems(root: Path) -> list[str]:
    """The three published manifests, byte for byte against the release contract.

    A version that agrees with itself is not enough: these documents are read by
    registries and plugin hosts, and a field that silently changes shape is a
    published mistake nobody can withdraw.
    """
    version = package_version(root)
    found = []

    if (root / "plugins/agentic-hil/.mcp.json").exists():
        found.append("plugin must not persist a portable bare or transient MCP server command")

    project = _project_table(root)
    if project.get("name") != "agentic-hil":
        found.append("Python package name must be agentic-hil")

    plugin_skill = _read(root, "plugins/agentic-hil/skills/agentic-hil/SKILL.md")
    required_plugin_guidance = [
        f'uv tool install --upgrade "agentic-hil=={version}"',
        "agentic-hil setup --agent claude",
    ]
    if any(command not in plugin_skill for command in required_plugin_guidance):
        found.append("plugin skill must pin the persistent package and delegate MCP registration to setup")

    registry = _json_document(root, "server.json")
    expected_registry = {
        "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        "name": "io.github.agentic-hil/agentic-hil",
        "title": "Agentic HIL",
        "description": "Policy-gated MCP tools for embedded hardware-in-the-loop testing on real devices.",
        "version": version,
        "repository": {
            "url": "https://github.com/agentic-hil/agentic-hil",
            "source": "github",
            "id": "1278450589",
        },
        "websiteUrl": "https://github.com/agentic-hil/agentic-hil",
        "packages": [
            {
                "registryType": "pypi",
                "registryBaseUrl": "https://pypi.org",
                "identifier": "agentic-hil",
                "version": version,
                "runtimeHint": "uvx",
                "transport": {"type": "stdio"},
                "packageArguments": [{"type": "positional", "value": "mcp-stdio"}],
            }
        ],
    }
    if registry != expected_registry:
        found.append("server.json differs from the locally validated release contract")

    expected_plugin = {
        "name": "agentic-hil",
        "displayName": "Agentic Hardware-in-the-Loop",
        "version": version,
        "description": (
            "Safe embedded firmware development with local hardware-in-the-loop targets, "
            "exposed as policy-gated MCP tools (probe, flash, reset, serial, CAN)."
        ),
        "author": {"name": "Hannes Pauli"},
        "repository": "https://github.com/agentic-hil/agentic-hil",
        "license": "Apache-2.0",
        "keywords": ["embedded", "firmware", "hardware-in-the-loop", "mcp", "stm32", "openocd"],
    }
    if _json_document(root, "plugins/agentic-hil/.claude-plugin/plugin.json") != expected_plugin:
        found.append("plugin.json differs from the locally validated release contract")

    expected_marketplace = {
        "name": "agentic-hil",
        "description": "Official Claude Code plugins for Agentic Hardware-in-the-Loop.",
        "owner": {"name": "agentic-hil"},
        "plugins": [
            {
                "name": "agentic-hil",
                "displayName": "Agentic Hardware-in-the-Loop",
                "description": (
                    "Safe embedded firmware development with hardware-in-the-loop targets "
                    "via policy-gated MCP tools."
                ),
                "source": "./plugins/agentic-hil",
                "version": version,
                "license": "Apache-2.0",
            }
        ],
    }
    if _json_document(root, ".claude-plugin/marketplace.json") != expected_marketplace:
        found.append("marketplace.json differs from the locally validated release contract")

    return found


def problems(root: Path, release_tag: str | None = None) -> list[str]:
    return [
        *version_problems(root, release_tag),
        *uncovered_files(root),
        *unclaimed_pins(root),
        *contract_problems(root),
    ]


def render_list(root: Path) -> str:
    """The checklist, printed from the check that enforces it."""
    version = package_version(root)
    carried = locations(root)
    occurrences = sum(len(location.versions) for location in carried)
    # Distinct paths, not entries: a file may hold several claims — TROUBLESHOOTING.md
    # carries requirement pins and a git-tag pin as two entries.
    files = len({location.path for location in carried})
    lines = [f"Release version {version} is carried in {files} files, {occurrences} occurrences:", ""]
    for location in carried:
        count = len(location.versions)
        suffix = "" if count == 1 else f"  [{count} occurrences]"
        lines.append(f"  {location.path}{suffix}")
        lines.append(f"      {location.carries}")
    lines.append("")
    lines.append("Deliberately not tracking the release:")
    for path, reason in sorted(UNTRACKED_MENTIONS.items()):
        lines.append(f"  {path}")
        lines.append(f"      {reason}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    root = REPOSITORY_ROOT
    release_tag = None
    listing = False
    while arguments:
        argument = arguments.pop(0)
        if argument == "--list":
            listing = True
        elif argument == "--release-tag":
            release_tag = arguments.pop(0) if arguments else None
        elif argument.startswith("--release-tag="):
            release_tag = argument.split("=", 1)[1]
        elif argument.startswith("--"):
            raise SystemExit(f"unknown option {argument}")
        else:
            root = Path(argument)
    if listing:
        print(render_list(root))
        return 0
    found = problems(root, release_tag)
    for problem in found:
        print(problem, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
