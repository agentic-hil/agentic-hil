from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from .adapters import adapter_for, build_agent_command
from .fixtures import prepare_fixture
from .refresh_login import export as export_refreshed
from .scrub_credentials import AUTH_PATHS, scrub
from .source import IGNORED_DIRECTORY_NAMES, IGNORED_FILE_SUFFIXES

HOME = Path("/home/eval")
WORKSPACE = Path("/workspace/project")
SOURCE = Path("/workspace/source")
MOUNTED_SOURCE = Path("/mnt/source")
MOUNTED_CREDENTIALS = Path("/run/agentic-hil-secrets")
TEMPORARY_CREDENTIALS = Path("/tmp/agentic-hil-credentials")
CREDENTIAL_KINDS = {
    "codex": {"codex-auth"},
    "claude-code": {"claude-auth"},
    "opencode": {"opencode-auth"},
}


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in IGNORED_DIRECTORY_NAMES or Path(name).suffix in IGNORED_FILE_SUFFIXES}


def prepare_workspace(job: dict) -> tuple[str, str]:
    WORKSPACE.mkdir(parents=True, exist_ok=False)
    (WORKSPACE / "README.md").write_text(
        "# Firmware project\n\nDisposable project used only for Agentic HIL installation evaluation.\n",
        encoding="utf-8",
    )

    target = job["target"]
    if target["mode"] == "local":
        if not MOUNTED_SOURCE.is_dir():
            raise RuntimeError("local target requires read-only source mount at /mnt/source")
        shutil.copytree(MOUNTED_SOURCE, SOURCE, symlinks=True, ignore=_copy_ignore)
        guide = str(SOURCE / "AI_AGENT_QUICKSTART.md")
        install_spec = str(SOURCE)
    else:
        guide = target["guide_url"]
        install_spec = target["install_spec"]
    return guide, install_spec


def sanitized_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "AGENTIC_HIL_CONFIG",
        "CONDA_DEFAULT_ENV",
        "CONDA_PREFIX",
        "PIP_PREFIX",
        "PIP_REQUIRE_VIRTUALENV",
        "PIP_TARGET",
        "PYTHONBREAKPOINT",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "VIRTUAL_ENV",
    ):
        environment.pop(name, None)
    environment["HOME"] = str(HOME)
    environment["USERPROFILE"] = str(HOME)
    environment["AGENTIC_HIL_EVAL"] = "1"
    return environment


def prepare_credential_files(agent: str, kinds: list[str]) -> None:
    for kind in kinds:
        if kind not in CREDENTIAL_KINDS[agent]:
            raise ValueError(f"credential kind {kind!r} is unsupported for {agent}")
        source = MOUNTED_CREDENTIALS / kind
        if not source.is_file():
            raise FileNotFoundError(f"credential mount missing: {source}")
        temporary = TEMPORARY_CREDENTIALS / kind
        temporary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source, temporary)
        temporary.chmod(0o600)
        target = AUTH_PATHS[kind]
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"credential destination already exists: {target}")
        target.symlink_to(temporary)


def cli_version(executable: str, environment: dict[str, str]) -> str:
    result = subprocess.run(
        [executable, "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
        env=environment,
    )
    return result.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", default="/job.json")
    args = parser.parse_args(argv)
    job = json.loads(Path(args.job).read_text(encoding="utf-8"))

    if os.geteuid() == 0:
        raise RuntimeError("install eval must not run as root")
    if shutil.which("agentic-hil") is not None:
        raise RuntimeError("agentic-hil is already installed before eval")

    adapter = adapter_for(job["agent"])
    prepare_fixture(adapter.id, job["case"]["fixture"], HOME)
    guide, install_spec = prepare_workspace(job)
    prompt = job["case"]["prompt_template"].format(
        workspace=WORKSPACE,
        guide=guide,
        install_spec=install_spec,
        agent=adapter.id,
    )
    credential_kinds = list(job.get("credential_file_kinds", []))
    try:
        prepare_credential_files(adapter.id, credential_kinds)
        environment = sanitized_environment()
        if adapter.id == "opencode":
            isolated_opencode_config = Path("/tmp/opencode-eval.json")
            isolated_opencode_config.write_text("{}\n", encoding="utf-8")
            environment["OPENCODE_CONFIG"] = str(isolated_opencode_config)
        executable = {
            "codex": "codex",
            "claude-code": "claude",
            "opencode": "opencode",
        }[adapter.id]
        metadata = {
            "event": "eval_start",
            "agent": adapter.id,
            "model": job["model"],
            "agent_cli_version": cli_version(executable, environment),
            "uid": os.geteuid(),
            "workspace": str(WORKSPACE),
        }
        print(json.dumps(metadata, sort_keys=True), flush=True)
        command = build_agent_command(adapter.id, job["model"], prompt)
        completed = subprocess.run(command, cwd=WORKSPACE, env=environment, check=False)
        print(json.dumps({"event": "eval_agent_exit", "exit_code": completed.returncode}), flush=True)
        if completed.returncode != 0:
            return completed.returncode

        # An agent CLI discovers skills when it starts, so the skill installed
        # during the session above is only in context for a fresh session.
        followup_template = job["case"].get("followup_prompt")
        if not followup_template:
            return completed.returncode
        followup = followup_template.format(
            workspace=WORKSPACE,
            guide=guide,
            install_spec=install_spec,
            agent=adapter.id,
        )
        print(json.dumps({"event": "eval_followup_start"}), flush=True)
        # The follow-up must see what setup registered, so it drops the
        # isolation that keeps the installation phase hermetic.
        followup_environment = dict(environment)
        followup_environment.pop("OPENCODE_CONFIG", None)
        second = subprocess.run(
            build_agent_command(adapter.id, job["model"], followup, isolated=False),
            cwd=WORKSPACE,
            env=followup_environment,
            check=False,
        )
        print(json.dumps({"event": "eval_followup_exit", "exit_code": second.returncode}), flush=True)
        return second.returncode
    finally:
        # The agent CLI may have refreshed a token. Stage it for the host while
        # this container's tmpfs still exists, then remove every copy.
        with contextlib.suppress(OSError):
            export_refreshed(credential_kinds)
        scrub(credential_kinds)


if __name__ == "__main__":
    raise SystemExit(main())
