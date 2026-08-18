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
TROUBLESHOOTING.md drifted by hand through four releases that way. Tag pins are
now an entry of their own, and `unclaimed_pins` refuses a covered file whose
project-tied `@v` pins its entries do not state.

A tree carries two versions, not one. Between releases `src/agentic_hil` moves
while the version string stands still, so for that whole window a working tree
shadows a published release: an install from it reports the released number, and
nothing downstream can tell the two apart. The first commit after each release
therefore moves the *distribution* version, which is what a build of this tree
calls itself, to a `.devN` development version, while every position that states
a FLOOR or an install pin for users keeps naming the release they can actually
install. `Location.tracks` says which of the two each position carries, and that
field is the whole of the rule: on a tree without a suffix the two versions are
the same string and everything behaves exactly as it did.

The sweep walks the working tree, which means it meets whatever else is sitting
in it, and a temporary directory is the thing most likely to be. pytest's
`--basetemp` can be pointed anywhere and on Windows it gets pointed inside the
clone on purpose: the automatic root nests a per-user directory, a numbered run
directory and a per-test directory before the fixture's own paths start, and
that is how a suite runs into MAX_PATH. So `.pytest-tmp` and the roots pytest
marks as its own are skipped here, not as a courtesy to the suite, but because
a version string under one of them is a fixture's scratch file. A single
focused run with `--basetemp=.pytest-tmp` used to fail this gate on a tree that
agreed with itself perfectly, which is a gate reporting on the developer's
command line rather than on the tree.

tomllib is deliberately absent: it is stdlib only from 3.11 and this project
still supports 3.10, so importing it would make the gate itself the thing that
fails on a matrix leg the developer machine never runs. `packaging` is absent for
a stronger version of the same reason, being no part of the standard library at
all, so the two version shapes this project uses are ordered here, in
`_ordering_key`.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

PACKAGE = "src/agentic_hil"

RELEASE_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
# The distribution version between releases. PEP 440 spells a development
# release `X.Y.Z.devN`, which sorts after every earlier release and before the
# `X.Y.Z` it anticipates.
DEVELOPMENT_VERSION = re.compile(r"^(?P<release>\d+\.\d+\.\d+)\.dev(?P<serial>\d+)$")
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
# The release each one-line installer installs, and the version an installation
# already on the machine has to reach to be left alone. Both are a single
# constant at the top of their script, and both have to follow the release: an
# installer holding a number below it answers "nothing to install" to the very
# person re-running the line to get current, and then registers the skill out of
# the copy it kept.
INSTALL_SH_RELEASE = re.compile(r'^RELEASE="(\d+\.\d+\.\d+)"$', re.MULTILINE)
INSTALL_PS1_RELEASE = re.compile(r"^\$Release = '(\d+\.\d+\.\d+)'$", re.MULTILINE)

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
# A pytest temporary root, wherever it landed. `--basetemp` may be pointed
# anywhere, and inside the clone is where a developer points it on Windows, to
# keep a deep scratch path off MAX_PATH; `PYTEST_DEBUG_TEMPROOT` lands the
# automatic root in the same place, always one level inside a `pytest-of-*`
# directory (`_pytest.tmpdir.TempPathFactory.getbasetemp`), which the name check
# below already catches. Everything below either one is a fixture's scratch file
# (a configuration a test wrote, a report it read back), so a version string in
# one of them is test data and not a position that tracks the release.
#
# Recognised by the conventional name and, for a root under a different name,
# by the numbered-directory shape `make_numbered_dir` actually leaves: a name
# ending in digits with a `<prefix>current` symlink beside it in the same
# parent, pointing back at this exact directory -- what `--basetemp`'s own
# `tmp_path` roots carry (`_pytest.pathlib.make_numbered_dir`). A bare `.lock`
# file used to be accepted as a standalone marker and was dropped: `.lock` is
# not pytest-specific, so a tracked directory that happened to hold one for an
# unrelated reason would silently drop out of this scan, version strings under
# it included. `.lock` is still asked for as an alternative to the symlink --
# it is what a numbered root under pytest's own default temp directory carries
# instead (`_pytest.pathlib.create_cleanup_lock`) -- but only once the
# numbered-directory name already holds, never standing in for it.
PYTEST_TEMPORARY_NAMES = frozenset({".pytest-tmp", ".pytest_tmp", "pytest-tmp"})
_PYTEST_NUMBERED_DIR = re.compile(r"^(?P<prefix>.+?)(?P<number>[0-9]+)$")
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
    # The container proof's comment names the release from which agent-install
    # accepts a home-directory cwd (#235), so a reader knows why the line is
    # typed in $HOME rather than a made-up subdirectory. Prose about a behaviour,
    # not a pin anything installs against.
    "tests/test_install_scripts.py": "prose reference to the release a behaviour changed in",
    # OpenOCD's own current release is 0.12.0. These state the debugger's
    # version, in its banner or as an example of one, and must never follow
    # ours; they became visible to the sweep the day our release number caught
    # up with OpenOCD's.
    ".github/ISSUE_TEMPLATE/bug_report.yml": "an example OpenOCD version, not this project's",
    "tests/fixtures/fake_openocd_missing_cfg.py": "the fake prints OpenOCD's own banner version",
    "tests/fixtures/fake_openocd_no_target.py": "the fake prints OpenOCD's own banner version",
    "tests/fixtures/fake_openocd_post_init_unconfirmed.py": "the fake prints OpenOCD's own banner version",
    "tests/fixtures/fake_openocd_unconfirmed.py": "the fake prints OpenOCD's own banner version",
    # NOT_CONTACTED's comment names the release that introduced the shape it
    # generalizes. History, not a pin.
    "src/agentic_hil/backends/common.py": "prose reference to the release a precedent shipped in",
    # One release changed what the tool refuses, so several documents and the
    # code itself state *from which release* a rule applies: the standard Windows
    # folders stopped being refused, a generated configuration started granting
    # everything, quarantine stopped firing where the failure proves it never
    # reached the board. A reader on 0.7.1 needs that sentence to know it does
    # not describe their installation. These name a release in prose and pin
    # nothing anything installs against; freezing them here keeps that history
    # readable instead of rewriting it at every bump.
    "README.md": "prose reference to the release a behaviour changed in",
    "SECURITY.md": "prose reference to the release a behaviour changed in",
    "docs/mcp-hosts.md": "prose reference to the release a behaviour changed in",
    "src/agentic_hil/config.py": "prose reference to the release a behaviour changed in",
    "src/agentic_hil/knowledge.py": "prose reference to the release a behaviour changed in",
    "src/agentic_hil/schemas/config.schema.json": "prose reference to the release a key was removed in",
    "tests/test_hardening.py": "prose reference to the release a behaviour changed in",
}


# Which of the two versions a position states. RELEASE is the release users can
# install: a floor, an install pin, a manifest a registry serves, the version an
# eval installs from the index. DISTRIBUTION is what a build of *this tree*
# calls itself, which between releases is ahead of the release and says so.
RELEASE = "release"
DISTRIBUTION = "distribution"


@dataclass(frozen=True)
class Location:
    """One place that carries a version, and which of the two it carries."""

    path: str
    carries: str
    versions: tuple[str, ...] = ()
    tracks: str = RELEASE


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
    """The distribution version: what a build of this tree calls itself.

    Either a strict SemVer release, on a release commit, or the `X.Y.Z.devN`
    that every commit between two releases carries.
    """
    version = _project_table(root).get("version")
    if version is None or not (RELEASE_VERSION.match(version) or DEVELOPMENT_VERSION.match(version)):
        raise SystemExit("pyproject.toml [project] declares no strict SemVer or X.Y.Z.devN version")
    return version


def is_development(version: str) -> bool:
    return DEVELOPMENT_VERSION.match(version) is not None


def _ordering_key(version: str) -> tuple[int, int, int, int, int]:
    """PEP 440 ordering for the two shapes this project uses.

    A development release sorts after every earlier release and before the
    release it anticipates: 1.2.3 < 1.2.4.dev0 < 1.2.4. That single ordering
    is what makes the development suffix safe to ship in a version string:
    every installer, resolver and `>=` floor already reads it this way.
    """
    development = DEVELOPMENT_VERSION.match(version)
    if development is not None:
        major, minor, patch = (int(part) for part in development.group("release").split("."))
        return (major, minor, patch, 0, int(development.group("serial")))
    major, minor, patch = (int(part) for part in version.split("."))
    return (major, minor, patch, 1, 0)


def is_newer(version: str, other: str) -> bool:
    return _ordering_key(version) > _ordering_key(other)


def next_development_version(release: str) -> str:
    """What the first commit after `release` moves the distribution version to."""
    major, minor, patch = (int(part) for part in release.split("."))
    return f"{major}.{minor}.{patch + 1}.dev0"


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
    sixth, `agentic-hil@v0.7.0` in a git URL, was invisible.
    """
    text = _read(root, relative)
    return tuple(match.group(2) for match in TAG_PIN.finditer(text) if "agentic-hil" in match.group(1))


def _changelog_release(root: Path) -> str:
    match = CHANGELOG_RELEASE.search(_read(root, "CHANGELOG.md"))
    if match is None:
        raise SystemExit("CHANGELOG.md has no released semantic version heading")
    return match.group(1)


def release_version(root: Path) -> str:
    """The released version every user-facing position states.

    On a release commit that is the distribution version itself, and nothing
    below can tell this tree from the one that shipped. On a tree that has moved
    on it is the newest CHANGELOG heading: the release a reader can still
    install, which every floor, pin, manifest and eval matrix keeps naming
    because they describe what an install gets and not what this tree builds.
    """
    version = package_version(root)
    if not is_development(version):
        return version
    return _changelog_release(root)


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
        Location(
            "pyproject.toml",
            "[project] version: the distribution version",
            (package_version(root),),
            tracks=DISTRIBUTION,
        ),
        Location(
            "src/agentic_hil/__init__.py",
            "__version__: what the CLI and MCP server report",
            (_init_version(root),),
            tracks=DISTRIBUTION,
        ),
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
        # git-tag pin, not a version requirement.
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
            "install.sh",
            "RELEASE: the release the one-line installer installs, and the version an installation already here has to reach to be kept",
            tuple(INSTALL_SH_RELEASE.findall(_read(root, "install.sh"))),
        ),
        Location(
            "install.ps1",
            "$Release: the release the one-line installer installs, and the version an installation already here has to reach to be kept",
            tuple(INSTALL_PS1_RELEASE.findall(_read(root, "install.ps1"))),
        ),
        Location(
            "evals/install/README.md",
            "expected_version in the documented matrix example a reader copies",
            tuple(EXPECTED_VERSION_FIELD.findall(_read(root, "evals/install/README.md"))),
        ),
    ]


def version_problems(root: Path, release_tag: str | None = None) -> list[str]:
    """Every version source that disagrees with the version it is supposed to state.

    Two comparisons where there used to be one, against the same list: a
    distribution position is held to what this tree builds, a release position
    to the release it describes. On a tree without a development suffix those
    are one string and this is the check it always was.
    """
    distribution = package_version(root)
    release = release_version(root)
    found = []
    if is_development(distribution) and not is_newer(distribution, release):
        found.append(
            f"CHANGELOG.md names {release} as the newest release, but pyproject.toml states development version "
            f"{distribution}, which does not follow it"
        )
    for location in locations(root):
        expected = distribution if location.tracks == DISTRIBUTION else release
        if not location.versions:
            found.append(f"{location.path} carries no version, but {location.carries} was expected")
            continue
        subject = "this tree builds" if location.tracks == DISTRIBUTION else "this release is"
        for stated in location.versions:
            if stated != expected:
                found.append(f"{location.path} states {stated}, but {subject} {expected} ({location.carries})")
    if release_tag:
        tagged = release_tag[1:] if release_tag.startswith("v") else release_tag
        if is_development(distribution):
            # A tag is cut from a release commit, and a release commit is the one
            # that sets the final number. A development version reaching this
            # path means the release was cut from a tree that never stopped
            # being between releases.
            found.append(
                f"release tag {release_tag} cannot be cut from a development tree: "
                f"pyproject.toml states {distribution}, and a tag never carries a development suffix"
            )
        elif tagged != distribution:
            found.append(f"release tag {release_tag} does not match pyproject.toml {distribution}")
    return found


def is_pytest_temporary_root(directory: Path) -> bool:
    """Whether this directory is a pytest temporary root rather than content.

    By name for the conventional in-clone basetemp and pytest's own default
    root, and otherwise by the one shape a directory `tmp_path_factory.mktemp`
    makes directly under an explicit `--basetemp` actually carries: a name
    ending in digits (`make_numbered_dir`'s own `<prefix><number>`) with a
    `<prefix>current` symlink beside it, in the same parent, resolving back to
    this exact directory. Pytest's own cleanup lock is not evidence here --
    `create_cleanup_lock` only ever marks a numbered root under pytest's
    default `pytest-of-*` temp directory, already caught by name above, so a
    `.lock` beside a numbered directory anywhere else says nothing about who
    made it and would exempt an ordinary tracked directory that merely ends in
    digits. Nothing in this repository is called `.pytest-tmp`, sits under a
    `pytest-of-` root, or matches that numbered-plus-symlink shape, so this can
    only ever exclude a temporary root.
    """
    if directory.name in PYTEST_TEMPORARY_NAMES or directory.name.startswith("pytest-of-"):
        return True
    match = _PYTEST_NUMBERED_DIR.match(directory.name)
    if match is None:
        return False
    current_link = directory.parent / f"{match.group('prefix')}current"
    if not current_link.is_symlink():
        return False
    try:
        return current_link.resolve() == directory.resolve()
    except OSError:
        return False


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
                if relative in SKIPPED_PATHS or is_pytest_temporary_root(entry):
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
    swept = (version,) if version else tuple(dict.fromkeys((release_version(root), package_version(root))))
    carries = [(one, re.compile(rf"(?<![\w.])v?{re.escape(one)}(?![\w.])")) for one in swept]
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
        for one, pattern in carries:
            if pattern.search(text):
                found.append(
                    f"{relative} carries version {one} but no check covers it: "
                    f"add it to locations() in tools/check_version_consistency.py, "
                    f"or to UNTRACKED_MENTIONS with the reason it must not track the release"
                )
                break
    return found


def unclaimed_pins(root: Path) -> list[str]:
    """Project-tied `@vX.Y.Z` pins in covered files that no entry states.

    `uncovered_files` deliberately skips a covered file, so a mention shape the
    file's extractors cannot see is checked by nothing — the gap that was
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

    Every version here is the release: a registry entry, a marketplace listing
    and an install command a reader copies all describe the release they can
    install, never the tree that is being worked on.
    """
    version = release_version(root)
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


def _git(root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Ask git, or answer that git could not be asked."""
    try:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _package_moved_since(root: Path, tag: str) -> bool | None:
    """Whether `src/agentic_hil` differs from what `tag` shipped. None: cannot say.

    `git diff` rather than a digest, so the answer comes through the line-ending
    normalisation `.gitattributes` pins; a raw comparison of an archive against a
    checkout reports every file as changed on a machine with core.autocrlf=true.
    A new file nothing has added yet is invisible to `git diff`, so it is asked
    for separately.
    """
    changed = _git(root, ["diff", "--quiet", tag, "--", PACKAGE])
    if changed is None or changed.returncode not in (0, 1):
        return None
    if changed.returncode == 1:
        return True
    untracked = _git(root, ["ls-files", "--others", "--exclude-standard", "--", PACKAGE])
    if untracked is None or untracked.returncode != 0:
        return None
    return bool(untracked.stdout.strip())


def moved_tree_findings(root: Path) -> tuple[list[str], list[str]]:
    """Refuse a tree that has moved past its release and does not say so.

    This is the other half of the two-version world, and the one that keeps it
    from being optional. A tree whose package has moved while its version stands
    still is indistinguishable from the release it shadows: an install from it
    reports the released number, `agentic-hil upgrade` calls it `already_current`,
    and the install eval refuses to run a local matrix at all. The remedy is the
    development suffix, so where the suffix is absent and the package has moved,
    this says so.

    Problems and warnings, because the question needs the release tag and not
    every checkout has one. A fork, a shallow clone and an unpacked sdist get the
    warning: an answer nothing here can give must not become a red build for
    people who did nothing wrong. Everything else in this module reads files;
    only this asks git, and a git that cannot answer is treated as an absent tag.
    """
    version = package_version(root)
    if is_development(version):
        return [], []
    tag = f"v{_changelog_release(root)}"
    top = _git(root, ["rev-parse", "--show-toplevel"])
    unchecked = [
        f"{tag} is not reachable here, so nothing checked whether {PACKAGE} still holds what that release "
        f"shipped; a tree that has moved past its release must carry a development version"
    ]
    if top is None or top.returncode != 0 or not top.stdout.strip():
        return [], unchecked
    # The top of a checkout, or the pathspec below is resolved against a
    # repository this tree merely sits inside, where it matches nothing and a
    # moved package would read as an unmoved one. samefile rather than a string
    # comparison: git answers with its own spelling of the path.
    try:
        inside = Path(top.stdout.strip()).samefile(root)
    except OSError:
        inside = False
    if not inside:
        return [], unchecked
    found = _git(root, ["rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}"])
    if found is None or found.returncode != 0 or not found.stdout.strip():
        return [], unchecked
    moved = _package_moved_since(root, tag)
    if moved is None:
        return [], unchecked
    if not moved:
        return [], []
    return [
        f"{PACKAGE} has moved since {tag} while the distribution version is still {version}, so this tree "
        f"shadows a published release: an install from it reports {version}, and nothing downstream can tell "
        f"the two apart. Move pyproject.toml and src/agentic_hil/__init__.py to a development version such as "
        f"{next_development_version(version)}; every other position keeps stating {version}"
    ], []


def problems(root: Path, release_tag: str | None = None) -> list[str]:
    return [
        *version_problems(root, release_tag),
        *uncovered_files(root),
        *unclaimed_pins(root),
        *contract_problems(root),
        *moved_tree_findings(root)[0],
    ]


def warnings(root: Path) -> list[str]:
    """What the gate could not establish here, as opposed to what it refuses."""
    return moved_tree_findings(root)[1]


def render_list(root: Path) -> str:
    """The checklist, printed from the check that enforces it."""
    distribution = package_version(root)
    release = release_version(root)
    carried = locations(root)
    occurrences = sum(len(location.versions) for location in carried)
    # Distinct paths, not entries: a file may hold several claims — TROUBLESHOOTING.md
    # carries requirement pins and a git-tag pin as two entries.
    files = len({location.path for location in carried})
    built = f" (built as {distribution})" if distribution != release else ""
    lines = [f"Release version {release}{built} is carried in {files} files, {occurrences} occurrences:", ""]
    for location in carried:
        count = len(location.versions)
        marks = []
        if count != 1:
            marks.append(f"{count} occurrences")
        if location.tracks == DISTRIBUTION:
            marks.append("the distribution version")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
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
    for note in warnings(root):
        print(f"WARNING: {note}", file=sys.stderr)
    for problem in found:
        print(problem, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
