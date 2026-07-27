from __future__ import annotations

import argparse
import shutil
from pathlib import Path

HOME = Path("/home/eval")
# Where a possibly refreshed login is staged for the host to collect. It must be
# gone before verification, which never sees a credential.
REFRESHED_DIRECTORY = HOME / ".agentic-hil-eval" / "refreshed"
AUTH_PATHS = {
    "codex-auth": HOME / ".codex" / "auth.json",
    "claude-auth": HOME / ".claude" / ".credentials.json",
    "opencode-auth": HOME / ".local" / "share" / "opencode" / "auth.json",
}


def scrub(kinds: list[str]) -> None:
    for kind in kinds:
        try:
            path = AUTH_PATHS[kind]
        except KeyError as error:
            raise ValueError(f"unsupported credential kind: {kind}") from error
        if path.is_symlink() or path.is_file():
            path.unlink()
        if path.exists():
            raise RuntimeError(f"credential path could not be scrubbed: {path}")

    shutil.rmtree(REFRESHED_DIRECTORY, ignore_errors=True)
    if REFRESHED_DIRECTORY.exists():
        raise RuntimeError(f"credential path could not be scrubbed: {REFRESHED_DIRECTORY}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kinds", nargs="+", required=True)
    args = parser.parse_args(argv)
    scrub(args.kinds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
