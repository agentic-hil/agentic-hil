from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .scrub_credentials import AUTH_PATHS, REFRESHED_DIRECTORY

BACKUP_SUFFIX = ".agentic-hil-eval.bak"


def export(kinds: list[str]) -> list[str]:
    """Copy each login out of the disposable home while the container still runs.

    An agent CLI that refreshes a token writes either through the symlink, which
    lands on tmpfs and dies with the container, or over it, which leaves a
    regular file in the home volume. Reading the resolved path covers both.
    """
    exported = []
    REFRESHED_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    for kind in kinds:
        source = AUTH_PATHS[kind]
        if not source.is_file():
            continue
        destination = REFRESHED_DIRECTORY / kind
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
        exported.append(kind)
    return exported


def dump(kinds: list[str]) -> dict[str, str]:
    """Read the exported logins out of the home volume, in a later container."""
    contents = {}
    for kind in kinds:
        path = REFRESHED_DIRECTORY / kind
        if path.is_file():
            contents[kind] = path.read_text(encoding="utf-8")
    return contents


def _has_text(node: Any, *path: str) -> bool:
    for key in path:
        if not isinstance(node, dict):
            return False
        node = node.get(key)
    return isinstance(node, str) and bool(node.strip())


def usable_login(kind: str, document: Any) -> bool:
    """Whether a returned document still looks like the login it replaces."""
    if not isinstance(document, dict):
        return False
    if kind == "claude-auth":
        return _has_text(document, "claudeAiOauth", "accessToken") and _has_text(
            document, "claudeAiOauth", "refreshToken"
        )
    if kind == "codex-auth":
        return _has_text(document, "tokens", "access_token") and _has_text(document, "tokens", "refresh_token")
    if kind == "opencode-auth":
        providers = [value for value in document.values() if isinstance(value, dict)]
        return any(_has_text(provider, "access") and _has_text(provider, "refresh") for provider in providers)
    return False


def apply_refreshed_login(kind: str, host_path: Path, content: str) -> str:
    """Replace the stored login with the refreshed one, keeping a backup.

    Returns what happened, never the token itself.
    """
    host_path = Path(host_path)
    try:
        document = json.loads(content)
    except ValueError:
        return "rejected: the returned login is not JSON"
    if not usable_login(kind, document):
        return "rejected: the returned login is missing its access or refresh token"

    current = host_path.read_text(encoding="utf-8") if host_path.is_file() else ""
    if current == content:
        return "unchanged"

    if current:
        shutil.copyfile(host_path, host_path.with_name(host_path.name + BACKUP_SUFFIX))
    temporary = host_path.with_name(host_path.name + ".agentic-hil-eval.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, host_path)
    return f"replaced; previous login kept at {host_path.name}{BACKUP_SUFFIX}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read refreshed logins out of the disposable home")
    parser.add_argument("--kinds", nargs="+", required=True)
    args = parser.parse_args(argv)
    json.dump(dump(args.kinds), sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
