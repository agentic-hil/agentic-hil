"""Write the build the old Read the Docs address serves: one moved notice per page.

The documentation is published at https://agentic-hil.github.io/docs/. It was
first published at https://agentic-hil.readthedocs.io/, and Read the Docs keeps
serving the last build of every active version for as long as the project
exists, so links into the old address kept landing on a copy that had stopped
changing, and whose canonical links named the old host rather than the pages
that replaced it.

`.readthedocs.yaml` runs this script instead of MkDocs. For every page the old
site served it writes a page at the same path that names the current page as
canonical, sends the browser there at once (a zero-second meta refresh, and a
script that keeps the fragment, since every old heading anchor exists on the new
page), and says so with a link for a reader whose browser follows neither. An
address the old site never served gets this build's 404 page, which links the
new site and redirects nowhere: there is no page to send it to, and sending it
to the home page would answer a broken link with a page that is not what it
asked for.

A redirect rule in the Read the Docs project settings answers the same addresses
with an HTTP 301, which no build can do, because a build only writes files and
a file is served with status 200. This build is what the old address shows where
that rule does not reach. See "The Documentation Site" in
docs/release-strategy.md, which carries the same table as MOVED_PAGES.

Standard library only: Read the Docs runs it with nothing installed.
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

NEW_SITE = "https://agentic-hil.github.io/docs/"

# Every page the old site served, by its path below the version root
# (/en/latest/ and /en/stable/), and the page of the current site that replaced
# it, by its path below NEW_SITE. Each old page kept its source file, its title
# and all of its heading anchors, so each one maps to the page of the same name.
MOVED_PAGES: tuple[tuple[str, str], ...] = (
    ("", ""),
    ("installation/", "installation/"),
    ("configuration/", "configuration/"),
    ("mcp-hosts/", "mcp-hosts/"),
    ("mcp-tools/", "mcp-tools/"),
    ("testing/", "testing/"),
    ("test-plan-contract/", "test-plan-contract/"),
    ("safety-model/", "safety-model/"),
    ("security-design/", "security-design/"),
    ("can-service-design/", "can-service-design/"),
    ("github-action-design/", "github-action-design/"),
    ("release-strategy/", "release-strategy/"),
    ("repository-security/", "repository-security/"),
)

STYLE = (
    ":root { color-scheme: light dark; }\n"
    "body { margin: 0; font: 1rem/1.5 system-ui, sans-serif; }\n"
    "main { max-width: 40rem; margin: 0 auto; padding: 3rem 1rem; }\n"
    "h1 { font-size: 1.5rem; line-height: 1.25; }\n"
    "a { overflow-wrap: anywhere; }\n"
)


def _document(head: str, body: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{head}"
        f"<style>\n{STYLE}</style>\n"
        "</head>\n"
        "<body>\n"
        "<main>\n"
        f"{body}"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )


def notice(target: str) -> str:
    """The page an old address serves: where its page is now, three ways."""
    url = html.escape(target)
    home = html.escape(NEW_SITE)
    head = (
        "<title>Moved to agentic-hil.github.io | Agentic HIL</title>\n"
        f'<link rel="canonical" href="{url}">\n'
        f'<meta http-equiv="refresh" content="0; url={url}">\n'
        f"<script>location.replace({json.dumps(target)} + location.hash);</script>\n"
    )
    body = (
        "<h1>The Agentic HIL documentation has moved</h1>\n"
        f'<p>This page is now at <a href="{url}">{url}</a>.</p>\n'
        f'<p>Every page of the documentation is published under <a href="{home}">{home}</a>.</p>\n'
    )
    return _document(head, body)


def not_found() -> str:
    """The 404 page: no redirect, because there is no page to redirect to."""
    home = html.escape(NEW_SITE)
    head = "<title>Page not found | Agentic HIL</title>\n"
    body = (
        "<h1>Page not found</h1>\n"
        "<p>There is no page at this address.</p>\n"
        f'<p>The Agentic HIL documentation is published under <a href="{home}">{home}</a>.</p>\n'
    )
    return _document(head, body)


def files() -> dict[str, str]:
    """Every file of the build, by its path below the version root."""
    site = {f"{old}index.html": notice(NEW_SITE + new) for old, new in MOVED_PAGES}
    site["404.html"] = not_found()
    return site


def write(output: Path) -> list[Path]:
    written = []
    for relative, text in files().items():
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: python tools/readthedocs_moved.py OUTPUT_DIRECTORY", file=sys.stderr)
        return 2
    written = write(Path(arguments[0]))
    print(f"Wrote {len(written)} files ({len(MOVED_PAGES)} moved notices and a 404 page) to {arguments[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
