from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from .adapters import EFFORT_FLAGS, adapter_for, build_agent_command
from .fixtures import (
    SKILL_NAME,
    prepare_fixture,
    prepare_workspace_fixture,
    registration_path,
    remove_registration_block,
    skills_directory,
)
from .refresh_login import export as export_refreshed
from .scrub_credentials import AUTH_PATHS, scrub
from .source import IGNORED_DIRECTORY_NAMES, IGNORED_FILE_SUFFIXES

HOME = Path("/home/eval")
WORKSPACE = Path("/workspace/project")
SOURCE = Path("/workspace/source")
MOUNTED_SOURCE = Path("/mnt/source")
MOUNTED_CREDENTIALS = Path("/run/eval-agent-logins")
TEMPORARY_CREDENTIALS = Path("/tmp/agentic-hil-credentials")
CREDENTIAL_KINDS = {
    "codex": {"codex-auth"},
    "claude-code": {"claude-auth"},
    "opencode": {"opencode-auth"},
}


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in IGNORED_DIRECTORY_NAMES or Path(name).suffix in IGNORED_FILE_SUFFIXES}


# An agent that stops to ask before touching user-level state is behaving well,
# and a one-shot prompt scores that as a failure to install. A real engineer
# answers, so the harness answers — as the operator, in a fresh session that has
# never seen the guide, addressing what agents have actually asked about rather
# than any one model's wording.
OPERATOR_REPLY = (
    "Go ahead and install Agentic HIL and set it up for this project. Answering what you may be "
    "weighing: the guide is at {guide}; the user-level MCP registration and the skill file are both "
    "intended, and I want them for every project on this machine; this is a disposable container of "
    "mine, so nothing here is precious; any agent login files you find under /run or /tmp are the test "
    "rig's, not the package's. If something still blocks you, name it and stop — but if it was only "
    "the scope, this is my decision and it is made."
)
# Two, because the reply is generic and a third round of the same words teaches
# an agent nothing it did not already decline.
MAX_OPERATOR_REPLIES = 2


def installed() -> bool:
    return shutil.which("agentic-hil", path=f"{HOME}/.local/bin:{os.environ.get('PATH', '')}") is not None


def prepare_workspace(job: dict) -> tuple[str, str]:
    WORKSPACE.mkdir(parents=True, exist_ok=False)
    # Naming the evaluation here was read as the reason not to act on it:
    # claude-sonnet-5 quoted this line back as evidence that the project had no
    # firmware and the request made no sense. A firmware project is what a
    # request to set up hardware tooling arrives in.
    (WORKSPACE / "README.md").write_text(
        "# Nucleo-F446RE firmware\n\nSTM32 Nucleo-F446RE target. Artifacts build into `build/`.\n",
        encoding="utf-8",
    )
    fixture = job["case"].get("workspace_fixture")
    if fixture:
        written = prepare_workspace_fixture(fixture, WORKSPACE)
        print(json.dumps({"event": "eval_workspace_fixture", "fixture": fixture, "files": written}), flush=True)

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


EFFORT_HELP = {
    "codex": ["codex", "exec", "--help"],
    "claude-code": ["claude", "--help"],
    "opencode": ["opencode", "run", "--help"],
}


def supports_reasoning_effort(agent: str, environment: dict[str, str]) -> bool:
    """Whether the CLI pinned in this image still has the mechanism we rely on.

    The image pins one version per CLI. A bump that renames or drops the flag
    must stop the run, not quietly produce a default-effort measurement wearing
    the label of the effort that was asked for.
    """
    result = subprocess.run(
        EFFORT_HELP[agent],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
        env=environment,
    )
    return EFFORT_FLAGS[agent] in result.stdout


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
    reasoning_effort = job["reasoning_effort"]
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
        version = cli_version(executable, environment)
        effort_supported = supports_reasoning_effort(adapter.id, environment) if reasoning_effort else None
        if reasoning_effort and not effort_supported:
            raise RuntimeError(
                f"job requests reasoning_effort={reasoning_effort!r} but this image's {adapter.id} "
                f"({version}) has no {EFFORT_FLAGS[adapter.id]}; the run would measure the default "
                "effort under the wrong label"
            )
        metadata = {
            "event": "eval_start",
            "agent": adapter.id,
            "model": job["model"],
            "reasoning_effort": reasoning_effort,
            "reasoning_effort_supported": effort_supported,
            "agent_cli_version": version,
            "uid": os.geteuid(),
            "workspace": str(WORKSPACE),
        }
        print(json.dumps(metadata, sort_keys=True), flush=True)
        command = build_agent_command(adapter.id, job["model"], prompt, reasoning_effort=reasoning_effort)
        completed = subprocess.run(command, cwd=WORKSPACE, env=environment, check=False)
        print(json.dumps({"event": "eval_agent_exit", "exit_code": completed.returncode}), flush=True)
        if completed.returncode != 0:
            return completed.returncode

        for attempt in range(1, MAX_OPERATOR_REPLIES + 1):
            if installed():
                break
            print(json.dumps({"event": "eval_operator_reply_start", "attempt": attempt}), flush=True)
            completed = subprocess.run(
                build_agent_command(
                    adapter.id, job["model"], OPERATOR_REPLY.format(guide=guide), reasoning_effort=reasoning_effort
                ),
                cwd=WORKSPACE,
                env=environment,
                check=False,
            )
            print(
                json.dumps(
                    {
                        "event": "eval_operator_reply_exit",
                        "attempt": attempt,
                        "exit_code": completed.returncode,
                        "installed": installed(),
                    }
                ),
                flush=True,
            )

        # An agent CLI discovers skills when it starts, so the skill installed
        # during the session above is only in context for a fresh session.
        followup_template = job["case"].get("followup_prompt")
        if not followup_template:
            return completed.returncode
        if not installed():
            # The follow-up measures how a hardware request is routed. Without an
            # installation there is nothing to route it through, so asking costs
            # a session and answers nothing.
            print(json.dumps({"event": "eval_followup_skipped", "reason": "nothing was installed"}), flush=True)
            return completed.returncode
        followup = followup_template.format(
            workspace=WORKSPACE,
            guide=guide,
            install_spec=install_spec,
            agent=adapter.id,
        )
        if job["case"].get("remove_skill_before_followup"):
            # The control arm. Uninstalling between the sessions leaves the MCP
            # registration setup wrote, so the follow-up measures the tools alone.
            skill = skills_directory(adapter.id, HOME) / SKILL_NAME
            shutil.rmtree(skill)
            # Codex reads its rules from AGENTS.md, not from a skills directory.
            # Leaving that block would hand the control arm the skill's routing
            # rules under another name.
            registration = registration_path(adapter.id, HOME)
            unregistered = remove_registration_block(registration)
            print(
                json.dumps(
                    {
                        "event": "eval_skill_removed",
                        "path": str(skill),
                        "registration_block_removed": unregistered,
                    }
                ),
                flush=True,
            )

        print(json.dumps({"event": "eval_followup_start"}), flush=True)
        # The follow-up must see what setup registered, so it drops the
        # isolation that keeps the installation phase hermetic.
        followup_environment = dict(environment)
        followup_environment.pop("OPENCODE_CONFIG", None)
        second = subprocess.run(
            build_agent_command(
                adapter.id, job["model"], followup, isolated=False, reasoning_effort=reasoning_effort
            ),
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
