"""The old Read the Docs address sends each of its pages to the page that replaced it.

`tools/readthedocs_moved.py` is the whole build Read the Docs runs now, and
nothing exercises it between the rare builds of the old address, so a target
that stopped existing on the new site would only show up as a notice pointing a
reader at a 404. These tests hold every target to a page of the `mkdocs.yml`
navigation, every notice to the three ways it names that page, the build
configuration to running nothing but the script, and the table in
docs/release-strategy.md to the script's.
"""

from __future__ import annotations

import ast
import re
import shlex
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

from readthedocs_moved import MOVED_PAGES, NEW_SITE, files, main  # noqa: E402

TABLE_ROW = re.compile(r"^\| `(?P<old>/[^`]*)` \| <(?P<new>https://[^>]+)> \|$")


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.canonical: list[str] = []
        self.refresh: list[str] = []
        self.robots: list[str] = []
        self.links: list[str] = []
        self.scripts: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "link" and values.get("rel") == "canonical":
            self.canonical.append(values.get("href", ""))
        elif tag == "meta" and values.get("http-equiv", "").lower() == "refresh":
            self.refresh.append(values.get("content", ""))
        elif tag == "meta" and values.get("name", "").lower() == "robots":
            self.robots.append(values.get("content", ""))
        elif tag == "a":
            self.links.append(values.get("href", ""))
        elif tag == "script":
            self._in_script = True
            self.scripts.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self.scripts[-1] += data


def _parse(text: str) -> _Page:
    page = _Page()
    page.feed(text)
    page.close()
    return page


def _mkdocs() -> dict:
    return yaml.safe_load((REPOSITORY_ROOT / "mkdocs.yml").read_text(encoding="utf-8"))


def _navigation_files(entries: list) -> list[str]:
    found = []
    for entry in entries:
        for value in entry.values() if isinstance(entry, dict) else [entry]:
            if isinstance(value, list):
                found.extend(_navigation_files(value))
            elif isinstance(value, str) and value.endswith(".md"):
                found.append(value)
    return found


def _published_path(source: str) -> str:
    """The path MkDocs serves a source file at, with its default directory URLs."""
    stem = source[: -len(".md")]
    if stem == "index":
        return ""
    if stem.endswith("/index"):
        return stem[: -len("index")]
    return f"{stem}/"


def test_the_notices_point_at_the_address_the_site_is_built_for() -> None:
    assert _mkdocs()["site_url"] == NEW_SITE


def test_every_target_is_a_page_of_the_navigation() -> None:
    """A target the site no longer has would send a reader from one dead end to another."""
    config = _mkdocs()
    assert "use_directory_urls" not in config, "_published_path assumes the MkDocs default"
    published = {_published_path(source) for source in _navigation_files(config["nav"])}
    for source in _navigation_files(config["nav"]):
        assert (REPOSITORY_ROOT / "docs" / source).is_file(), source

    missing = [new for _, new in MOVED_PAGES if new not in published]

    assert not missing, f"no page of the mkdocs.yml navigation is published at {missing}"


def test_only_the_old_home_page_is_sent_to_the_home_page() -> None:
    """A blanket redirect to the start page answers a link with a page it did not ask for."""
    old_paths = [old for old, _ in MOVED_PAGES]

    assert len(old_paths) == len(set(old_paths))
    assert ("", "") in MOVED_PAGES
    for old, new in MOVED_PAGES:
        assert old == "" or new != "", f"{old} is sent to the home page"
        for path in (old, new):
            assert path == "" or (path.endswith("/") and not path.startswith("/")), path


def test_every_old_page_names_its_new_page_three_ways() -> None:
    """Canonical for search engines, an immediate redirect for browsers, a link for everyone else.

    No noindex: it would contradict the canonical, and a search engine that
    honours it drops the old address without learning where its page went.
    """
    site = files()

    for old, new in MOVED_PAGES:
        target = NEW_SITE + new
        page = _parse(site[f"{old}index.html"])

        assert page.canonical == [target], old
        assert page.refresh == [f"0; url={target}"], old
        assert page.scripts == [f'location.replace("{target}" + location.hash);'], old
        assert target in page.links, old
        assert page.robots == [], old


def test_the_build_has_the_index_page_read_the_docs_requires() -> None:
    """Read the Docs fails a build whose HTML output has no index.html at its root."""
    assert "index.html" in files()


def test_an_address_the_old_site_never_served_is_not_redirected() -> None:
    page = _parse(files()["404.html"])

    assert page.refresh == []
    assert page.scripts == []
    assert page.canonical == []
    assert NEW_SITE in page.links


def test_read_the_docs_runs_the_script_and_builds_nothing_else() -> None:
    """A `mkdocs` or `sphinx` key would make Read the Docs build and publish a second copy."""
    config = yaml.safe_load((REPOSITORY_ROOT / ".readthedocs.yaml").read_text(encoding="utf-8"))

    assert config["version"] == 2
    assert {"mkdocs", "sphinx", "python", "conda"}.isdisjoint(config)
    assert "commands" not in config["build"]
    assert config["build"]["os"]
    assert config["build"]["tools"]["python"]
    assert config["build"]["jobs"] == {
        "build": {"html": ['python tools/readthedocs_moved.py "$READTHEDOCS_OUTPUT/html"']}
    }


def test_the_configured_command_writes_every_notice(tmp_path: Path) -> None:
    """Run the command `.readthedocs.yaml` names, from the repository root, as Read the Docs does."""
    config = yaml.safe_load((REPOSITORY_ROOT / ".readthedocs.yaml").read_text(encoding="utf-8"))
    (command,) = config["build"]["jobs"]["build"]["html"]
    arguments = shlex.split(command.replace("$READTHEDOCS_OUTPUT", tmp_path.as_posix()))
    assert arguments[0] == "python"

    completed = subprocess.run(
        [sys.executable, *arguments[1:]], cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
    output = tmp_path / "html"
    assert sorted(path.relative_to(output).as_posix() for path in output.rglob("*.html")) == sorted(files())
    for relative, text in files().items():
        assert (output / relative).read_text(encoding="utf-8") == text


def test_the_command_refuses_to_guess_where_to_write(capsys) -> None:
    assert main([]) == 2
    assert "usage" in capsys.readouterr().err


def test_the_script_runs_with_the_standard_library_only() -> None:
    """Read the Docs runs it with nothing installed, on whatever Python `.readthedocs.yaml` names."""
    source = (REPOSITORY_ROOT / "tools" / "readthedocs_moved.py").read_text(encoding="utf-8")
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])

    assert modules <= {"__future__", "html", "json", "pathlib", "sys"}, sorted(modules)


def test_the_release_strategy_table_is_the_scripts_table() -> None:
    """The documented mapping and the built one are the same rows in the same order."""
    text = (REPOSITORY_ROOT / "docs" / "release-strategy.md").read_text(encoding="utf-8")
    section = text.split("\n## The Documentation Site\n", 1)[1].split("\n## ", 1)[0]
    rows = [match.groups() for line in section.splitlines() if (match := TABLE_ROW.match(line))]

    assert rows == [(f"/{old}", NEW_SITE + new) for old, new in MOVED_PAGES]
