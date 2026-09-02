from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from .scrub_credentials import AUTH_PATHS, REFRESHED_DIRECTORY

BACKUP_SUFFIX = ".agentic-hil-eval.bak"

# Every file this module creates holds an access and a refresh token, so it is
# readable by its owner and by nobody else. `Path.write_text` and
# `shutil.copyfile` create through the process umask instead, which on a shared
# machine is usually 0644: the login the CLI itself keeps at 0600 would be world
# readable for as long as the copy lives, and the backup lives until somebody
# deletes it (#391).
PRIVATE_MODE = 0o600


def private_mode(path: Path) -> int:
    """The mode a copy of `path` may carry: owner-only, and never wider than it.

    Windows has no mode here worth carrying over -- a regular file stats 0666
    and a read-only one 0444 -- so a copy is created owner-writable there and
    what keeps it private is the profile directory it sits in, the same argument
    the rest of this repository's secret files rest on.
    """
    if os.name == "nt":
        return PRIVATE_MODE
    try:
        current = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return PRIVATE_MODE
    # Narrower than 0600 is the caller's own choice and is kept; a login with no
    # owner bits at all is not a mode to reproduce onto a file we then have to
    # read back.
    return (current & PRIVATE_MODE) or PRIVATE_MODE


def _create_private(path: Path, mode: int) -> int:
    """Open `path` fresh, carrying `mode` from the moment it exists.

    A chmod after the create is a window, and for a file holding a refresh token
    it is a window on every other account on the machine. `O_EXCL` after the
    unlink so this can only ever be a create: it must never inherit the mode of
    whatever was sitting on the name.
    """
    with suppress(FileNotFoundError):
        os.unlink(path)
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)


def write_private_text(path: Path, text: str, mode: int = PRIVATE_MODE) -> None:
    """What `Path.write_text` writes, on a file that was never world readable."""
    descriptor = _create_private(path, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(text)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def copy_private(source: Path, destination: Path, mode: int | None = None) -> None:
    """What `shutil.copyfile` copies, with the copy no wider than the original.

    `mode` names it outright where the copy is this module's own staging file
    rather than a stand-in for the caller's own login.
    """
    data = source.read_bytes()
    descriptor = _create_private(destination, private_mode(source) if mode is None else mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


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
        copy_private(source, destination, PRIVATE_MODE)
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
        if _has_text(document, "tokens", "access_token") and _has_text(document, "tokens", "refresh_token"):
            return True
        # An API key is a supported Codex login in its own right: it carries no
        # OAuth tokens because it has nothing to refresh, and `credential_health`
        # already accepts one at the preflight. The return validator has to agree,
        # or an unchanged, valid API-key auth.json is rejected on the way back out
        # of every container even though nothing in it was ever spent.
        return _has_text(document, "OPENAI_API_KEY")
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

    # Both copies carry the mode of the file being replaced, taken before it is,
    # and `os.replace` renames rather than rewrites, so the login that lands is
    # the one the temporary already was.
    mode = private_mode(host_path)
    if current:
        copy_private(host_path, host_path.with_name(host_path.name + BACKUP_SUFFIX), mode)
    temporary = host_path.with_name(host_path.name + ".agentic-hil-eval.tmp")
    write_private_text(temporary, content, mode)
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
