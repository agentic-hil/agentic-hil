"""Capture real macOS framework-Python and installer hash-tool facts for #518."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
from datetime import date
from pathlib import Path
from typing import Any


def path_facts(value: str | Path) -> dict[str, Any]:
    path = Path(value).expanduser()
    is_symlink = path.is_symlink()
    return {
        "path": str(path),
        "realpath": str(path.resolve()),
        "is_symlink": is_symlink,
        "link_target": os.readlink(path) if is_symlink else None,
    }


def command_output(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def output_has_digest(output: str, digest: str) -> bool:
    """Whether a shasum or OpenSSL response contains the computed SHA-256."""
    return re.search(rf"\b{re.escape(digest)}\b", output, re.IGNORECASE) is not None


def capture(wheel_path: Path) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise RuntimeError("this recording must be captured on macOS")
    run_id = os.environ.get("GITHUB_RUN_ID")
    repository = os.environ.get("GITHUB_REPOSITORY")
    commit = os.environ.get("GITHUB_SHA")
    if not run_id or not repository or not commit:
        raise RuntimeError("capture requires GitHub Actions run id, repository and commit provenance")
    framework = sysconfig.get_config_var("PYTHONFRAMEWORK")
    preferred = sysconfig.get_preferred_scheme("user")
    if not framework or preferred != "osx_framework_user":
        raise RuntimeError(
            "runner Python is not a macOS framework installation: "
            f"PYTHONFRAMEWORK={framework!r}, preferred user scheme={preferred!r}"
        )
    if sys.prefix != sys.base_prefix:
        raise RuntimeError("the capture interpreter must be the base framework Python, not a virtual environment")
    if not wheel_path.is_file():
        raise RuntimeError(f"wheel does not exist: {wheel_path}")

    wheel_path = wheel_path.resolve()
    wheel_digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    homebrew_version = command_output(["brew", "--version"])
    python_formula = command_output(["brew", "list", "--versions", "python@3.13"])
    if homebrew_version["returncode"] != 0 or python_formula["returncode"] != 0:
        raise RuntimeError(f"could not record the Homebrew Python installation: {homebrew_version}, {python_formula}")
    schemes = {}
    for name in (preferred, "posix_user"):
        purelib = sysconfig.get_path("purelib", name)
        platlib = sysconfig.get_path("platlib", name)
        schemes[name] = {
            "purelib": path_facts(purelib),
            "platlib": path_facts(platlib),
        }

    tools: dict[str, Any] = {}
    for name, version_command, hash_command in (
        ("shasum", ["shasum", "--version"], ["shasum", "-a", "256", str(wheel_path)]),
        ("openssl", ["openssl", "version"], ["openssl", "dgst", "-sha256", str(wheel_path)]),
    ):
        executable = shutil.which(name)
        if executable is None:
            raise RuntimeError(f"required hashing tool is missing from PATH: {name}")
        version = command_output(version_command)
        hashed = command_output(hash_command)
        if version["returncode"] != 0 or hashed["returncode"] != 0:
            raise RuntimeError(f"{name} failed: version={version}, sha256={hashed}")
        if not output_has_digest(hashed["stdout"], wheel_digest):
            raise RuntimeError(f"{name} digest disagrees with Python hashlib: {hashed['stdout']!r}")
        tools[name] = {"executable": path_facts(executable), "version": version, "sha256": hashed}

    return {
        "what": "Real macOS framework Python layout and hashing-tool output over this run's built agentic-hil wheel.",
        "recorded_on": date.today().isoformat(),
        "platform": "macOS",
        "platform_version": platform.platform(),
        "github_actions": {
            "repository": repository,
            "run_id": run_id,
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "commit": commit,
            "ref": os.environ.get("GITHUB_REF"),
            "run_url": (
                f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                f"{repository}/actions/runs/{run_id}"
            ),
        },
        "method": {
            "python_install": "brew install python@3.13",
            "wheel_build": '"$RUNNER_TEMP/agentic-hil-build/bin/python" -m build --wheel --outdir dist',
            "homebrew_version": homebrew_version,
            "python_formula": python_formula,
        },
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
            "executable_path": path_facts(sys.executable),
            "prefix": sys.prefix,
            "base_prefix": sys.base_prefix,
            "framework": framework,
            "preferred_user_scheme": preferred,
            "schemes": schemes,
        },
        "wheel": {
            "path": str(wheel_path),
            "filename": wheel_path.name,
            "size": wheel_path.stat().st_size,
            "sha256": wheel_digest,
        },
        "tools": tools,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path, help="the wheel built by this workflow run")
    parser.add_argument("--output", required=True, type=Path, help="where to write the JSON recording")
    args = parser.parse_args()
    recording = capture(args.wheel)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(recording, indent=2) + "\n", encoding="utf-8")
    print(f"Recorded macOS installer facts in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
