from __future__ import annotations

import argparse
import collections
import contextlib
import dataclasses
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Case, CredentialFile, Job, Matrix, Target, load_matrix
from .credentials import authentication_failure, credential_health, stored_secrets
from .redaction import DEFAULT_LOG_CONTENT_BYTES, RedactingLogWriter, redact, redact_value
from .refresh_login import apply_refreshed_login
from .report import group_counts, report_results, result_group, timings_results, unstable_groups
from .routing import routing_results
from .source import create_source_snapshot, source_digest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DOCKERFILE = REPOSITORY_ROOT / "evals" / "install" / "container" / "Dockerfile"
CONTAINER_DIRECTORY = DEFAULT_DOCKERFILE.parent
AGENT_CLI_PACKAGES = {
    "codex": "@openai/codex",
    "claude-code": "@anthropic-ai/claude-code",
    "opencode": "opencode-ai",
}
TOOL_CONTRACT = Path("evals") / "install" / "tools.list.expected"
# PEP 440's development release, which is what this tree carries between two
# releases. `tools/check_version_consistency.py` owns the rule; here it is only
# the shape that matters, because the question is what an install reports.
DEVELOPMENT_VERSION = re.compile(r"^\d+\.\d+\.\d+\.dev\d+$")
# The tag that names a release. Published mode reads these to decide which
# release this clone can stand for, so a pre-release or a `.devN` is deliberately
# not a match; a published `expected_version` is then held against this exact set
# of tags rather than reparsed, so a version with no tag here has no reference.
RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")


def _release_version_key(version: str) -> tuple[int, ...]:
    """Order releases by number, so v0.9.10 follows v0.9.2 rather than preceding it."""
    return tuple(int(part) for part in version.split("."))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip(".-") or "run"
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{cleaned[:61]}-{suffix}"


def git_metadata(source_root: Path) -> dict[str, Any]:
    try:
        revision = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(source_root), "status", "--porcelain"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=True,
            ).stdout.strip()
        )
        return {"revision": revision, "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"revision": None, "dirty": None}


def source_version(source_root: Path) -> str:
    """What an install from this tree calls itself.

    Not necessarily `target.expected_version`: between releases the tree carries
    a development version while the matrix keeps naming the release, and this is
    the number every artifact of a local install will actually report.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
        import tomli as tomllib

    data = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("source pyproject.toml declares no [project] version")
    return version


def validate_source_version(source_root: Path, expected_version: str) -> str:
    """Refuse a matrix this tree cannot answer for, and say what it installs as.

    `target.expected_version` names the release, because the same field is what
    a published-mode run pins and what the version gate holds every matrix to.
    Between releases the tree is ahead of that release and says so with a
    development suffix, so the two legitimately differ; which development
    version follows which release is settled by
    `tools/check_version_consistency.py` on every push, and the only thing worth
    refusing here is a tree that is neither the release nor a development
    version of anything.
    """
    version = source_version(source_root)
    if version != expected_version and DEVELOPMENT_VERSION.match(version) is None:
        raise ValueError(f"source version {version!r} does not match target.expected_version {expected_version!r}")
    return version


def installed_version(target: Target, host_source_root: Path) -> str:
    """The version an install for this target will report, and so what to expect.

    Published mode installs the release from the index, and the release is what
    the matrix names. Every other mode installs this tree: local from a snapshot
    of it, remote from the commit `validate_remote_checkout` has already pinned
    it to. What such an install reports is the tree's own version, development
    suffix and all, and expecting anything else would fail every run in a cycle
    or, worse, pass a tree off as the release it is not.
    """
    if target.mode == "published":
        return target.expected_version
    return source_version(host_source_root)


def validate_remote_checkout(source_root: Path, expected_commit: str) -> None:
    metadata = git_metadata(source_root)
    revision = metadata.get("revision")
    if not isinstance(revision, str) or revision.lower() != expected_commit.lower():
        raise ValueError(
            f"remote evaluation requires --source-root checked out at {expected_commit}; found {revision or '<unknown>'}"
        )
    result = subprocess.run(
        [
            "git",
            "-C",
            str(source_root),
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "pyproject.toml",
            "src/agentic_hil",
            str(TOOL_CONTRACT),
        ],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "could not verify remote source checkout")
    if result.stdout.strip():
        raise ValueError(
            "remote evaluation requires clean pyproject.toml, src/agentic_hil, "
            "and evals/install/tools.list.expected at expected commit"
        )


def expected_mcp_tools(source_root: Path) -> list[str]:
    contract = source_root / TOOL_CONTRACT
    if contract.is_symlink() or not contract.is_file():
        raise ValueError(f"target MCP tool contract must be a regular non-symlink file: {contract}")
    names = [line.strip() for line in contract.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not names:
        raise ValueError(f"target MCP tool contract is empty: {contract}")
    if names != sorted(names) or len(names) != len(set(names)):
        raise ValueError(f"target MCP tool contract must be sorted and unique: {contract}")
    invalid = [name for name in names if re.fullmatch(r"[a-z][a-z0-9_]*", name) is None]
    if invalid:
        raise ValueError(f"target MCP tool contract contains invalid names: {invalid}")
    return names


def committed_package_digest(repository: Path, commit: str) -> str:
    """Digest the committed package, not the working tree beside it.

    A tree checked out with CRLF hashes differently from what pip fetches from
    the remote, and the first branch-mode matrix failed all 72 reachable runs on
    that alone.
    """
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "package.tar"
        result = run_capture(
            ["git", "-C", str(repository), "archive", "--format=tar", "-o", str(archive), commit, "src/agentic_hil"],
            120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"could not export {commit} from {repository}")
        extracted = Path(directory) / "tree"
        with tarfile.open(archive) as bundle:
            # The safe extraction filter is the default from 3.12 and absent on
            # the oldest Python this project supports.
            if hasattr(tarfile, "data_filter"):
                bundle.extractall(extracted, filter="data")
            else:
                bundle.extractall(extracted)
        return source_digest(extracted / "src" / "agentic_hil")


def released_package_digest(repository: Path, version: str) -> str | None:
    """What ``src/agentic_hil`` held at the tag this version names, if it is here.

    None means the clone carries no such tag, so nothing offline can say what the
    release shipped and the caller has no gate to apply.
    """
    tag = f"v{version}"
    found = run_capture(["git", "-C", str(repository), "rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}"], 30)
    if found.returncode != 0 or not found.stdout.strip():
        return None
    return committed_package_digest(repository, tag)


def validate_source_matches_release(source_root: Path, expected_version: str) -> None:
    """Refuse a local matrix whose package moved while its version stood still.

    Local mode installs this tree and every artifact then reports it under
    ``expected_version``. When the tree has moved past the tag that version names,
    the whole matrix measures a build nobody released while labelling it a
    release, and the digest the verifier compares against is the moved tree's
    own: self-consistent, and about the wrong thing.

    Only for a tree that still claims the release. A tree carrying a development
    version reports that version, from the wheel to the MCP server banner, so
    there is no longer a release for it to be mistaken for and the caller does
    not ask.

    The released digest comes from the tag in this clone rather than from the
    index, so the gate holds offline. A clone without the tag cannot answer, and
    says so rather than passing silently. Comparing an archive against a working
    tree needs the repository to pin its line endings, which this one does in
    `.gitattributes`; without that pin the two differ on Windows for that reason
    alone, and the digest the verifier compares against would already be wrong
    for the same reason.
    """
    working = source_digest(source_root / "src" / "agentic_hil")
    released = released_package_digest(source_root, expected_version)
    if released is None:
        print(
            f"WARNING: this clone has no v{expected_version} tag, so the matrix cannot check that "
            f"src/agentic_hil still holds what that version shipped",
            file=sys.stderr,
        )
        return
    if released != working:
        raise ValueError(
            f"src/agentic_hil has moved since v{expected_version} while the version stayed "
            f"{expected_version}: working tree {working}, v{expected_version} {released}. Every run "
            f"would install this tree and report it as the release. Give the working tree a "
            f"development version suffix, or run the matrix in published mode against the release."
        )


def release_tag_versions(repository: Path) -> list[str]:
    """Every `vX.Y.Z` release tag in this clone, oldest first, or [] when none.

    Published mode installs whatever the package index serves as the current
    release, and the runner cannot ask the index offline. The newest tag here is
    the only thing it can read as "the current release", so it is what a published
    `target.expected_version` is held against. A malformed or pre-release tag is
    not a release and is dropped; an empty list means this clone names no release.
    """
    result = run_capture(["git", "-C", str(repository), "tag", "--list", "v[0-9]*"], 30)
    if result.returncode != 0:
        return []
    versions = [
        line.strip()[1:] for line in result.stdout.splitlines() if RELEASE_TAG.fullmatch(line.strip())
    ]
    return sorted(versions, key=_release_version_key)


def validate_published_release_is_current(source_root: Path, expected_version: str) -> None:
    """Refuse a published run this clone cannot verify offline.

    Published mode hands the agent the guide and nothing else, so the install is
    the guide's own unpinned "current release"; the verifier then requires the
    installed distribution and the running server to report exactly
    `target.expected_version`, and holds the installed bytes and the agent skill
    against `src/agentic_hil` as it stood at the `v{expected_version}` tag in this
    clone. No source is mounted here, so that tag is the only trusted reference
    there is offline. Two things have to hold, or every run spends model time and
    API budget on a verdict it can never reach:

    - The tag has to be here at all. Without it `released_package_digest` returns
      None: nothing offline can say what the release shipped, the installed bytes
      are not staged as trusted, and `matching agent skill installed` is left with
      no copy to compare against and fails every run. A non-release version, a
      clone carrying no release tags, and a version newer than the newest tag all
      land here.
    - It has to be the current release. Offline, the newest `vX.Y.Z` tag in this
      clone is what the runner reads as current: a version older than it names a
      release the index has moved past, so the guide's unpinned install of the
      current release would fail the exact-version checks.

    Both reduce to one rule: the pinned version must be the newest release tag in
    this clone. A clone whose tags are behind the index cannot evaluate a newer
    release offline; it is told to fetch the tag rather than being passed through
    to a run that cannot reach a verdict.
    """
    versions = release_tag_versions(source_root)
    if not versions:
        raise ValueError(
            f"published target.expected_version {expected_version} cannot be evaluated: this clone "
            f"carries no vX.Y.Z release tag, so nothing offline can say what the release shipped and the "
            f"installed bytes and agent skill would have no trusted reference to be held against. Fetch "
            f"the release tags into this clone, then rerun."
        )
    newest = versions[-1]
    if expected_version not in versions:
        raise ValueError(
            f"published target.expected_version {expected_version} has no matching v{expected_version} "
            f"release tag in this clone (newest is v{newest}), so nothing offline can say what it shipped "
            f"and matching agent skill installed would fail every run. Pin the current release v{newest}, "
            f"or fetch the tag for {expected_version} into this clone if the index already serves it."
        )
    if expected_version != newest:
        raise ValueError(
            f"published target.expected_version {expected_version} names a release this clone has "
            f"already superseded with v{newest}. Published mode installs the current release the guide "
            f"names, unpinned, and the verifier holds the install to exactly {expected_version}, so the "
            f"current release (v{newest} or newer) would fail every run. Pin the current release, or "
            f"measure an older one only in a mode that installs exactly it."
        )


def source_gates(host_source_root: Path, target: Target) -> str:
    """Everything the source tree must satisfy before the first container starts.

    One place, because these refusals are one question asked of one tree, and the
    answer to the first decides whether the second applies: a tree carrying a
    development version reports itself rather than the release, which is the
    entire defect `validate_source_matches_release` exists to refuse. Left in, it
    would refuse the ordinary state of every commit between two releases. Published
    mode adds its own: the release pinned there has to be the one the guide's
    unpinned install actually produces, which offline is the newest tag here.
    """
    version = validate_source_version(host_source_root, target.expected_version)
    if target.mode == "local" and version == target.expected_version:
        validate_source_matches_release(host_source_root, target.expected_version)
    if target.mode == "published":
        validate_published_release_is_current(host_source_root, target.expected_version)
    if target.mode == "remote":
        assert target.expected_commit is not None
        validate_remote_checkout(host_source_root, target.expected_commit)
    return version


def job_payload(
    matrix: Matrix,
    case: Case,
    job: Job,
    source_root: Path | None,
    host_source_root: Path,
) -> dict[str, Any]:
    target = dataclasses.asdict(matrix.target)
    # What the verifier compares every artifact against: the version this
    # target's install actually produces, which for anything but published mode
    # is the tree's own and not the release the matrix names.
    target["expected_version"] = installed_version(matrix.target, host_source_root)
    if matrix.target.mode == "local":
        if source_root is None:
            raise ValueError("target requires trusted source evidence")
        target["expected_package_digest"] = source_digest(source_root / "src" / "agentic_hil")
    elif matrix.target.mode == "published":
        # A released wheel is built, not checked out, so no tree here digests to
        # it directly -- but when this clone carries the release tag,
        # `released_package_digest` archives `src/agentic_hil` from that commit
        # the same way `validate_source_matches_release` already trusts it for
        # local mode, giving the verifier a real reference to hold the
        # installed bytes to instead of a check nothing can ever satisfy. A
        # clone without the tag still names no reference; the verifier treats
        # that as unverified rather than as a mismatch. A real run never reaches
        # that state -- `source_gates` refuses a published target whose tag is
        # missing before the matrix starts -- but a dry run and this function's
        # direct callers do not go through the gate, so the None stays handled.
        target["expected_package_digest"] = released_package_digest(host_source_root, matrix.target.expected_version)
    else:
        target["expected_package_digest"] = committed_package_digest(host_source_root, matrix.target.expected_commit)
    target["source_git"] = git_metadata(host_source_root)
    tool_names = expected_mcp_tools(host_source_root)
    target["expected_mcp_tools"] = tool_names
    target["expected_mcp_tools_digest"] = hashlib.sha256(
        ("\n".join(tool_names) + "\n").encode("utf-8")
    ).hexdigest()
    if matrix.target.mode == "local":
        if source_root is None:
            raise ValueError("local target requires source_root")
        target["source_digest"] = source_digest(source_root)
    return {
        "schema_version": 1,
        "executor": "docker",
        "agent": job.agent,
        "model": job.model,
        "reasoning_effort": job.reasoning_effort,
        "repetition": job.repetition,
        "timeout_seconds": job.timeout_seconds,
        "credential_file_kinds": [credential.kind for credential in job.credential_files],
        "case": dataclasses.asdict(case),
        "target": target,
    }


def docker_security_options() -> list[str]:
    return [
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "512",
        # Sampled across 105 containers of a full matrix: the busiest one peaked
        # at 654 MiB, nine in ten stayed under 568 MiB. The old 4g was six times
        # what any run used, and a cap that large stops being a guard against a
        # runaway container once a dozen of them share one host.
        "--memory",
        "2g",
        "--cpus",
        "2",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,nodev,size=1g",
    ]


def docker_mount(kind: str, source: str, destination: str, *, readonly: bool = False) -> str:
    parts = [f"type={kind}", f"src={source}", f"dst={destination}"]
    if readonly:
        parts.append("readonly")
    return ",".join(parts)


def agent_container_command(
    *,
    docker: str,
    image: str,
    container_name: str,
    home_volume: str,
    workspace_volume: str,
    job_file: Path,
    source_root: Path | None,
    credentials: tuple[str, ...],
    credential_files: tuple[tuple[str, Path], ...] = (),
) -> list[str]:
    command = [
        docker,
        "run",
        "--rm",
        "--name",
        container_name,
        *docker_security_options(),
        "--mount",
        docker_mount("volume", home_volume, "/home/eval"),
        "--mount",
        docker_mount("volume", workspace_volume, "/workspace"),
        "--mount",
        docker_mount("bind", str(job_file.resolve()), "/job.json", readonly=True),
    ]
    if source_root is not None:
        command.extend(
            [
                "--mount",
                docker_mount("bind", str(source_root.resolve()), "/mnt/source", readonly=True),
            ]
        )
    for kind, path in credential_files:
        command.extend(
            [
                "--mount",
                docker_mount(
                    "bind",
                    str(path),
                    f"/run/eval-agent-logins/{kind}",
                    readonly=True,
                ),
            ]
        )
    for name in credentials:
        if os.environ.get(name):
            command.extend(["--env", name])
    return [*command, image, "--job", "/job.json"]


def verifier_container_command(
    *,
    docker: str,
    image: str,
    container_name: str,
    home_volume: str,
    workspace_volume: str,
    job_file: Path,
) -> list[str]:
    return [
        docker,
        "run",
        "--rm",
        "--name",
        container_name,
        *docker_security_options(),
        "--network",
        "none",
        "--workdir",
        "/opt",
        "--mount",
        docker_mount("volume", home_volume, "/home/eval", readonly=True),
        "--mount",
        docker_mount("volume", workspace_volume, "/workspace", readonly=True),
        "--mount",
        docker_mount("bind", str(job_file.resolve()), "/job.json", readonly=True),
        "--entrypoint",
        "/opt/install-eval-verifier/bin/python",
        image,
        "-m",
        "evals.install.verifier",
        "--job",
        "/job.json",
    ]


def credential_export_command(
    *,
    docker: str,
    image: str,
    container_name: str,
    home_volume: str,
    kinds: tuple[str, ...],
) -> list[str]:
    """Read the staged logins out of the home volume, without network or write access."""
    return [
        docker,
        "run",
        "--rm",
        "--name",
        container_name,
        *docker_security_options(),
        "--network",
        "none",
        "--workdir",
        "/opt",
        "--mount",
        docker_mount("volume", home_volume, "/home/eval", readonly=True),
        "--entrypoint",
        "/usr/bin/python3",
        image,
        "-m",
        "evals.install.refresh_login",
        "--kinds",
        *kinds,
    ]


def credential_scrubber_command(
    *,
    docker: str,
    image: str,
    container_name: str,
    home_volume: str,
    kinds: tuple[str, ...],
) -> list[str]:
    return [
        docker,
        "run",
        "--rm",
        "--name",
        container_name,
        *docker_security_options(),
        "--network",
        "none",
        "--workdir",
        "/opt",
        "--mount",
        docker_mount("volume", home_volume, "/home/eval"),
        "--entrypoint",
        "/usr/bin/python3",
        image,
        "-m",
        "evals.install.scrub_credentials",
        "--kinds",
        *kinds,
    ]


def docker_subprocess_environment(docker: str) -> dict[str, str]:
    """Keep Docker Desktop's sibling credential helpers discoverable."""
    environment = os.environ.copy()
    docker_path = Path(docker)
    if not docker_path.is_absolute():
        return environment

    docker_directory = str(docker_path.parent)
    path_key = next((key for key in environment if key.casefold() == "path"), "PATH")
    path_entries = [entry for entry in environment.get(path_key, "").split(os.pathsep) if entry]
    normalized_directory = os.path.normcase(os.path.normpath(docker_directory))
    if all(
        os.path.normcase(os.path.normpath(entry.strip('"'))) != normalized_directory
        for entry in path_entries
    ):
        path_entries.insert(0, docker_directory)
    environment[path_key] = os.pathsep.join(path_entries)
    return environment


def run_logged(
    command: list[str],
    log_path: Path,
    *,
    timeout_seconds: int,
    secrets: list[str],
    idle_timeout_seconds: int | None = None,
    timeout_container: str | None = None,
    log_content_byte_limit: int = DEFAULT_LOG_CONTENT_BYTES,
) -> tuple[int, str | None, str | None]:
    """Run a container, streaming its output, and report why it was stopped.

    The second element is None, "total" or "idle". A provider that stops
    answering leaves a process that is alive and silent, which the total budget
    only notices at its very end: an hour of wall clock for a run that died in
    its first seconds. The idle budget stops that in minutes.
    """
    process = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=64 * 1024,
        errors="replace",
        env=docker_subprocess_environment(command[0]),
    )
    chunks: queue.Queue[str | None] = queue.Queue(maxsize=16)

    def reader() -> None:
        assert process.stdout is not None
        try:
            while chunk := process.stdout.read(64 * 1024):
                chunks.put(chunk)
        finally:
            chunks.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    started = time.monotonic()
    last_output = started
    timed_out: str | None = None
    timeout_cleanup_error: str | None = None
    stream_closed = False
    with log_path.open("w", encoding="utf-8") as handle:
        log_writer = RedactingLogWriter(handle, secrets, content_byte_limit=log_content_byte_limit)
        while not stream_closed or process.poll() is None:
            now = time.monotonic()
            exhausted = now - started > timeout_seconds
            stalled = idle_timeout_seconds is not None and now - last_output > idle_timeout_seconds
            if not timed_out and process.poll() is None and (exhausted or stalled):
                timed_out = "total" if exhausted else "idle"
                if timeout_container:
                    try:
                        remove_container(command[0], timeout_container)
                    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                        timeout_cleanup_error = (
                            f"container {timeout_container}: timeout removal failed: "
                            f"{type(error).__name__}: {error}"
                        )
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            try:
                item = chunks.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                stream_closed = True
                continue
            last_output = time.monotonic()
            log_writer.feed(item)
        log_writer.finish()
    return process.wait(), timed_out, timeout_cleanup_error


def run_capture(command: list[str], timeout_seconds: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
        env=docker_subprocess_environment(command[0]),
    )


def extract_start_metadata(log_path: Path) -> dict[str, Any]:
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("event") == "eval_start":
            return value
    return {}


# Claude Code accepts an unknown --effort, warns on stderr, and continues with
# exit 0. run_logged merges stderr into agent.log, so this warning is the one
# hard piece of evidence that a requested effort never reached the model.
EFFORT_IGNORED = "Unknown --effort value"
# What each CLI calls the reasoning tokens it counted. Only integers match, so a
# field carrying reasoning text cannot be mistaken for a count.
REASONING_TOKEN_PATTERN = re.compile(r'"(reasoning_output_tokens|reasoning_tokens|reasoning)"\s*:\s*(\d+)')
# Two silences are a provider that is down, not a run that was unlucky.
STALLS_BEFORE_UNCAPPED = 2
# Two runs of one agent share one login file on this machine. A provider rotates
# the refresh token when it hands out a new one, so a concurrent write-back can
# leave the copy here rejected, which is how a stored login was lost once
# already. Writing back is serialized; the runs themselves are not.
_LOGIN_WRITE_BACK = threading.Lock()


def reasoning_tokens_reported(text: str) -> int | None:
    """How many reasoning tokens the CLI itself counted, if it counts them.

    codex writes reasoning_output_tokens and opencode writes reasoning; Claude
    Code folds thinking into output_tokens and reports no separate number, so
    None means the CLI cannot answer rather than that nothing was reasoned.
    """
    found = REASONING_TOKEN_PATTERN.findall(text)
    if not found:
        return None
    return sum(int(value) for _, value in found)


# Where each CLI reports what a turn consumed, and what it calls the field. codex
# writes turn.completed with a usage object, opencode writes step_finish with a
# tokens object; Claude Code reports total_cost_usd in its own result line, which
# keeps flowing through the transcript untouched. Nothing here prices anything:
# a rate card belongs to whoever reads these counts, not to the harness that
# records them, and a price baked in here would go stale silently.
TOKEN_EVENTS = {
    "turn.completed": "usage",
    "step_finish": "tokens",
    "step-finish": "tokens",
}
TOKEN_FIELDS = {
    "input": ("input_tokens", "input"),
    "cached_input": ("cached_input_tokens", "cached_input"),
    "output": ("output_tokens", "output"),
    "reasoning": ("reasoning_output_tokens", "reasoning_tokens", "reasoning"),
}


def _usage_payloads(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, list):
        for item in node:
            found.extend(_usage_payloads(item))
        return found
    if not isinstance(node, dict):
        return found
    field = TOKEN_EVENTS.get(str(node.get("type")))
    if field is not None and isinstance(node.get(field), dict):
        found.append(node[field])
    for value in node.values():
        found.extend(_usage_payloads(value))
    return found


def _token_counts(payload: dict[str, Any]) -> dict[str, int]:
    flattened = dict(payload)
    cache = payload.get("cache")
    if isinstance(cache, dict) and isinstance(cache.get("read"), int):
        # opencode reports the cached prefix as a read against its cache.
        flattened.setdefault("cached_input", cache["read"])
    counts: dict[str, int] = {}
    for name, aliases in TOKEN_FIELDS.items():
        for alias in aliases:
            value = flattened.get(alias)
            if isinstance(value, int) and not isinstance(value, bool):
                counts[name] = value
                break
    return counts


def token_usage(log_path: Path) -> dict[str, int] | None:
    """What the agent CLI counted for this run, summed over its turns.

    A run is two or three sessions, so per-turn counts are added up: the question
    a reader asks of this field is what the run consumed, not what one turn of it
    did. None means this CLI reported no counts at all, which is not the same as
    a run that consumed nothing.
    """
    if not log_path.is_file():
        return None
    totals: dict[str, int] = {}
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            document = json.loads(line)
        except ValueError:
            continue
        for payload in _usage_payloads(document):
            for name, value in _token_counts(payload).items():
                totals[name] = totals.get(name, 0) + value
    return totals or None


def reasoning_effort_evidence(log_path: Path, requested: str | None) -> tuple[bool, str]:
    """Whether anything in this run contradicts the reasoning effort it asked for.

    This does not prove the requested level ran; it looks for a contradiction of
    it. A CLI that accepts the flag and silently ignores it would otherwise
    produce a default-effort measurement wearing the requested label. Where the
    CLI counts reasoning tokens the count can answer it: across 356 measured runs
    every transcript that carried the field reported between 127 and 8762, never
    zero, so a zero is the CLI running the model without the reasoning that was
    asked for. That count is a run total across both sessions, so reasoning spent
    installing can hide a measured session that did none. Where the CLI does not
    count them, absence of a contradiction is all there is, and the detail says
    which of the two this is.
    """
    if requested is None:
        return True, "no reasoning effort requested"
    if not log_path.is_file():
        return False, "no transcript to check the requested effort against"
    text = log_path.read_text(encoding="utf-8", errors="replace")
    if EFFORT_IGNORED in text:
        return False, f"the agent CLI reported it ignored the requested effort {requested}"
    reported = reasoning_tokens_reported(text)
    if reported is None:
        return True, f"requested {requested}; this CLI reports no reasoning count and the transcript does not contradict it"
    if reported == 0:
        return False, f"requested {requested}, but the CLI counted 0 reasoning tokens for the whole run"
    return True, f"requested {requested}; the CLI counted {reported} reasoning tokens"


def create_volume(docker: str, name: str) -> None:
    result = run_capture([docker, "volume", "create", "--label", "agentic-hil.eval=true", name], 30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"could not create Docker volume {name}")


def _docker_resource_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    detail = f"{result.stdout}\n{result.stderr}".lower()
    return "no such" in detail or "not found" in detail


def _docker_resource_exists(docker: str, kind: str, name: str) -> bool:
    result = run_capture([docker, kind, "inspect", name], 30)
    if result.returncode == 0:
        return True
    if _docker_resource_not_found(result):
        return False
    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    raise RuntimeError(f"could not inspect Docker {kind} {name}: {detail}")


def _remove_docker_resource(docker: str, kind: str, name: str) -> None:
    if not _docker_resource_exists(docker, kind, name):
        return
    result = run_capture([docker, kind, "rm", "--force", name], 30)
    if result.returncode != 0 and not _docker_resource_not_found(result):
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"could not remove Docker {kind} {name}: {detail}")
    if _docker_resource_exists(docker, kind, name):
        raise RuntimeError(f"Docker {kind} {name} still exists after removal")


def remove_volume(docker: str, name: str) -> None:
    _remove_docker_resource(docker, "volume", name)


def remove_container(docker: str, name: str) -> None:
    _remove_docker_resource(docker, "container", name)


def image_digest(docker: str, image: str) -> str | None:
    result = run_capture([docker, "image", "inspect", "--format", "{{.Id}}", image], 30)
    return result.stdout.strip() if result.returncode == 0 else None


def parse_verification(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    for line in reversed(result.stdout.splitlines()):
        with_json = line.strip()
        if not with_json:
            continue
        try:
            value = json.loads(with_json)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("schema_version") == 1:
            return value
    raise ValueError(result.stderr.strip() or "verifier did not return JSON")


def credential_file_path(credential: CredentialFile) -> Path:
    raw = os.environ.get(credential.path_environment)
    if not raw:
        raise ValueError(f"missing credential file environment: {credential.path_environment}")
    configured = Path(raw).expanduser()
    if not configured.is_absolute():
        raise ValueError(f"{credential.path_environment} must contain an absolute file path")
    try:
        path = configured.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"{credential.path_environment} cannot be resolved: {error}") from error
    if not path.is_file():
        raise ValueError(f"{credential.path_environment} does not identify a regular file")
    try:
        path.relative_to(REPOSITORY_ROOT.resolve())
    except ValueError:
        pass
    else:
        raise ValueError(f"{credential.path_environment} must point outside the repository")
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError(f"{credential.path_environment} exceeds 2 MiB")
    if "," in str(path):
        raise ValueError(f"{credential.path_environment} path may not contain a comma")
    return path


def resolved_credential_files(job: Job) -> tuple[tuple[str, Path], ...]:
    return tuple((credential.kind, credential_file_path(credential)) for credential in job.credential_files)


def auth_values(job: Job) -> list[str]:
    # `stored_secrets` is the same pass the loop wrapper's pre-flight uses on a
    # CLI's own output, so a token has one definition of what must not be
    # printed rather than one per surface that prints.
    values = [os.environ[name] for name in job.credentials if os.environ.get(name)]
    for _kind, path in resolved_credential_files(job):
        values.extend(stored_secrets(path))
    return list(dict.fromkeys(value for value in values if value))


AUTHENTICATION_LOG_CHARS = 8000


def agent_authentication_failure(run_directory: Path) -> str | None:
    """Whether the agent CLI refused to start because its login is unusable."""
    log_path = run_directory / "agent.log"
    if not log_path.is_file():
        return None
    text = log_path.read_text(encoding="utf-8", errors="replace")[-AUTHENTICATION_LOG_CHARS:]
    return authentication_failure(text)


def ensure_auth(job: Job) -> None:
    missing = [name for name in job.credentials if not os.environ.get(name)]
    if missing:
        names = ", ".join(missing)
        raise ValueError(f"{job.agent}/{job.model}: missing configured credential environment: {names}")
    resolved_credential_files(job)


def run_one(
    *,
    docker: str,
    matrix: Matrix,
    case: Case,
    job: Job,
    source_root: Path | None,
    host_source_root: Path,
    output_root: Path,
    image_id: str | None,
    refresh_login: bool = False,
) -> dict[str, Any]:
    # Only inserted when there is one, so runs without an effort keep the run
    # ids earlier artifacts already carry.
    effort_segment = f"-{job.reasoning_effort}" if job.reasoning_effort else ""
    run_id = slug(f"{case.id}-{job.agent}-{job.model}{effort_segment}-r{job.repetition}")
    run_directory = output_root / run_id
    if run_directory.exists():
        raise FileExistsError(f"run output already exists: {run_directory}")
    run_directory.mkdir(parents=True)
    payload = job_payload(matrix, case, job, source_root, host_source_root)
    job_file = run_directory / "job.json"
    job_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    token = uuid.uuid4().hex[:12]
    home_volume = f"agentic-hil-eval-{token}-home"
    workspace_volume = f"agentic-hil-eval-{token}-workspace"
    agent_container = f"agentic-hil-eval-{token}-agent"
    scrubber_container = f"agentic-hil-eval-{token}-scrub"
    verifier_container = f"agentic-hil-eval-{token}-verify"
    credential_files = resolved_credential_files(job)
    redaction_secrets = [os.environ[name] for name in job.credentials if os.environ.get(name)]
    for _kind, path in credential_files:
        redaction_secrets.extend((str(path), path.as_posix()))
    credentials_scrubbed = not credential_files
    created_volumes: list[str] = []
    cleanup_errors: list[str] = []
    cleanup_resources: set[str] = set()
    unresolved_containers: set[str] = set()
    unresolved_volumes: set[str] = set()
    started_at = utc_now()
    started = time.monotonic()
    status = "error"
    failure: str | None = None
    effort_ok = True
    effort_detail = "not evaluated"
    agent_exit_code: int | None = None
    verifier_exit_code: int | None = None
    verification: dict[str, Any] | None = None

    def record_cleanup_error(kind: str, name: str, error: object) -> None:
        cleanup_resources.add(name)
        cleanup_errors.append(f"{kind} {name}: {type(error).__name__}: {error}")

    try:
        redaction_secrets = list(dict.fromkeys((*redaction_secrets, *auth_values(job))))
        create_volume(docker, home_volume)
        created_volumes.append(home_volume)
        create_volume(docker, workspace_volume)
        created_volumes.append(workspace_volume)
        agent_command = agent_container_command(
            docker=docker,
            image=matrix.image,
            container_name=agent_container,
            home_volume=home_volume,
            workspace_volume=workspace_volume,
            job_file=job_file,
            source_root=source_root if matrix.target.mode == "local" else None,
            credentials=job.credentials,
            credential_files=credential_files,
        )
        agent_exit_code, timed_out, timeout_cleanup_error = run_logged(
            agent_command,
            run_directory / "agent.log",
            timeout_seconds=job.timeout_seconds,
            idle_timeout_seconds=job.idle_timeout_seconds,
            secrets=redaction_secrets,
            timeout_container=agent_container,
        )
        if timeout_cleanup_error:
            cleanup_resources.add(agent_container)
            cleanup_errors.append(timeout_cleanup_error)
            unresolved_containers.add(agent_container)
            raise RuntimeError(timeout_cleanup_error)
        if timed_out == "idle":
            failure = (
                f"agent produced no output for {job.idle_timeout_seconds}s and was stopped after "
                f"{round(time.monotonic() - started)}s"
            )
        elif timed_out:
            failure = f"agent timed out after {job.timeout_seconds}s"

        if credential_files and refresh_login:
            # The agent CLI may have refreshed a token; collect it before the
            # scrubber removes every copy, and put it back where it came from.
            export_result = run_capture(
                credential_export_command(
                    docker=docker,
                    image=matrix.image,
                    container_name=f"{agent_container}-export",
                    home_volume=home_volume,
                    kinds=tuple(kind for kind, _path in credential_files),
                ),
                60,
            )
            if export_result.returncode == 0:
                with contextlib.suppress(ValueError), _LOGIN_WRITE_BACK:
                    returned = json.loads(export_result.stdout or "{}")
                    for kind, path in credential_files:
                        content = returned.get(kind)
                        if not isinstance(content, str) or not content.strip():
                            continue
                        redaction_secrets = list(dict.fromkeys((*redaction_secrets, content)))
                        print(f"{kind}: {apply_refreshed_login(kind, path, content)}", file=sys.stderr)

        if credential_files:
            scrubber_command = credential_scrubber_command(
                docker=docker,
                image=matrix.image,
                container_name=scrubber_container,
                home_volume=home_volume,
                kinds=tuple(kind for kind, _path in credential_files),
            )
            scrubber_result = run_capture(scrubber_command, 60)
            if scrubber_result.returncode != 0:
                raise RuntimeError(scrubber_result.stderr.strip() or "credential scrubber failed")
            credentials_scrubbed = True

        verifier_command = verifier_container_command(
            docker=docker,
            image=matrix.image,
            container_name=verifier_container,
            home_volume=home_volume,
            workspace_volume=workspace_volume,
            job_file=job_file,
        )
        verifier_result = run_capture(verifier_command, 180)
        verifier_exit_code = verifier_result.returncode
        with (run_directory / "verifier.stderr.log").open("w", encoding="utf-8") as handle:
            verifier_log = RedactingLogWriter(handle, redaction_secrets)
            verifier_log.feed(verifier_result.stderr)
            verifier_log.finish()
        verification = parse_verification(verifier_result)
        verification_for_report = redact_value(verification, redaction_secrets)
        (run_directory / "verification.json").write_text(
            json.dumps(verification_for_report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        effort_ok, effort_detail = reasoning_effort_evidence(run_directory / "agent.log", job.reasoning_effort)
        expected_safe_failure = case.expected_outcome == "safe-failure"
        agent_ok = agent_exit_code == 0 or expected_safe_failure
        if not timed_out and agent_ok and effort_ok and verifier_exit_code == 0 and verification.get("ok") is True:
            status = "passed"
        elif agent_exit_code != 0 and (phrase := agent_authentication_failure(run_directory)):
            # A dead login is not a result about the installation guide.
            status = "error"
            failure = f"agent could not authenticate ({phrase}); the stored login needs to be renewed"
        elif timed_out == "idle":
            # Neither is a provider that stopped answering. Counting it as a
            # failure would read like a finding about the guide, and the
            # repetition that disagrees with it will be run again anyway.
            status = "error"
        else:
            status = "failed"
            if not effort_ok:
                failure = failure or f"reasoning effort not applied: {effort_detail}"
            failure = failure or "agent or independent verification failed"
    except Exception as error:
        failure = redact(f"{type(error).__name__}: {error}", redaction_secrets)
    finally:
        for container in (agent_container, verifier_container, scrubber_container):
            try:
                remove_container(docker, container)
                unresolved_containers.discard(container)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                unresolved_containers.add(container)
                record_cleanup_error("container", container, error)
        if (
            credential_files
            and not credentials_scrubbed
            and home_volume in created_volumes
            and agent_container not in unresolved_containers
        ):
            try:
                scrubber_result = run_capture(
                    credential_scrubber_command(
                        docker=docker,
                        image=matrix.image,
                        container_name=scrubber_container,
                        home_volume=home_volume,
                        kinds=tuple(kind for kind, _path in credential_files),
                    ),
                    60,
                )
                credentials_scrubbed = scrubber_result.returncode == 0
                if not credentials_scrubbed:
                    detail = scrubber_result.stderr.strip() or scrubber_result.stdout.strip() or "unknown error"
                    raise RuntimeError(detail)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                unresolved_volumes.add(home_volume)
                record_cleanup_error("credential volume", home_volume, error)
            finally:
                try:
                    remove_container(docker, scrubber_container)
                    unresolved_containers.discard(scrubber_container)
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    unresolved_containers.add(scrubber_container)
                    record_cleanup_error("container", scrubber_container, error)

        # Raw agent evidence may contain copied environment or file credentials.
        # Never retain it as a Docker volume.
        for volume in reversed(created_volumes):
            try:
                remove_volume(docker, volume)
                unresolved_volumes.discard(volume)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                unresolved_volumes.add(volume)
                record_cleanup_error("volume", volume, error)

        if cleanup_errors:
            status = "error"
            cleanup_failure = "cleanup failed: " + "; ".join(cleanup_errors)
            failure = f"{failure}; {cleanup_failure}" if failure else cleanup_failure

    result = {
        "schema_version": 1,
        "kind": "install",
        "executor": "docker",
        "id": run_id,
        "status": status,
        "agent": {
            "cli": job.agent,
            "model": job.model,
            "reasoning_effort": job.reasoning_effort,
            "cli_version": extract_start_metadata(run_directory / "agent.log").get("agent_cli_version")
            if (run_directory / "agent.log").is_file()
            else None,
        },
        "case": {"id": case.id, "expected_outcome": case.expected_outcome},
        # Raw counts as the CLI reported them, or null where it reports none.
        "tokens": token_usage(run_directory / "agent.log"),
        "target": payload["target"],
        "environment": {
            "image": matrix.image,
            "image_digest": image_id,
            "credential_file_links_scrubbed": credentials_scrubbed if credential_files else None,
            "home_volume": home_volume if home_volume in unresolved_volumes else None,
            "workspace_volume": workspace_volume if workspace_volume in unresolved_volumes else None,
        },
        "cleanup": {
            "ok": not cleanup_errors,
            "errors": cleanup_errors,
            "resources": sorted(cleanup_resources),
            "remaining_containers": sorted(unresolved_containers),
            "remaining_volumes": sorted(unresolved_volumes),
        },
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "agent_exit_code": agent_exit_code,
        "verifier_exit_code": verifier_exit_code,
        "failure": failure,
        # The verifier has no access to agent.log, so the effort check is made
        # here and reported beside the verifier's own checks.
        "checks": [
            *(verification.get("checks", []) if verification else []),
            *(
                []
                if job.reasoning_effort is None
                else [{"name": "requested reasoning effort not contradicted", "ok": effort_ok, "detail": effort_detail}]
            ),
        ],
        "artifacts": {
            "agent_log": "agent.log",
            "job": "job.json",
            "verification": "verification.json" if verification else None,
            "verifier_stderr": "verifier.stderr.log",
        },
    }
    result = redact_value(result, redaction_secrets)
    (run_directory / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def dry_run_plan(
    matrix: Matrix,
    source_root: Path | None,
    host_source_root: Path,
    docker: str,
) -> dict[str, Any]:
    plans: list[dict[str, Any]] = []
    for case in matrix.cases:
        for job in matrix.jobs:
            payload = job_payload(matrix, case, job, source_root, host_source_root)
            placeholder = Path("/absolute/job.json")
            credential_file_placeholders = tuple(
                (
                    credential.kind,
                    Path(f"/HOST_SECRET_FROM_{credential.path_environment}"),
                )
                for credential in job.credential_files
            )
            plans.append(
                {
                    "case": case.id,
                    "agent": job.agent,
                    "model": job.model,
                    "repetition": job.repetition,
                    "payload": payload,
                    "forwarded_environment_names": [name for name in job.credentials if os.environ.get(name)],
                    "forwarded_credential_file_kinds": [credential.kind for credential in job.credential_files],
                    "agent_command": agent_container_command(
                        docker=docker,
                        image=matrix.image,
                        container_name="agentic-hil-eval-DRYRUN-agent",
                        home_volume="agentic-hil-eval-DRYRUN-home",
                        workspace_volume="agentic-hil-eval-DRYRUN-workspace",
                        job_file=placeholder,
                        source_root=source_root if matrix.target.mode == "local" else None,
                        credentials=job.credentials,
                        credential_files=credential_file_placeholders,
                    ),
                }
            )
    return {"schema_version": 1, "image": matrix.image, "runs": plans}


def model_key(job: Job) -> tuple[str, str, str | None]:
    return (job.agent, job.model, job.reasoning_effort)


def interleaved_plan(planned: list[tuple[Case, Job]]) -> list[tuple[Case, Job]]:
    """Order runs so neighbouring slots hold different models.

    Longest-first alone puts the same model into every worker at once, because
    equal expected time is mostly a sign of the same model. Measured with six
    workers: four claude-sonnet-4-6 runs started inside 21 seconds and all four
    then produced nothing for 300 s, though that model finishes a run in under
    a minute when it goes alone. Rotating between models keeps the slow work
    early without pointing the whole pool at one provider.
    """
    groups: dict[tuple[str, str, str | None], list[tuple[Case, Job]]] = {}
    for pair in planned:
        groups.setdefault(model_key(pair[1]), []).append(pair)

    def expected(pair: tuple[Case, Job]) -> float:
        # Unknown counts as slow so an unmeasured combination cannot become the
        # tail that leaves every other worker idle at the end.
        return pair[1].expected_seconds or float("inf")

    for members in groups.values():
        members.sort(key=expected, reverse=True)
    rotation = sorted(groups.values(), key=lambda members: max(map(expected, members)), reverse=True)
    ordered: list[tuple[Case, Job]] = []
    for index in range(max((len(members) for members in rotation), default=0)):
        ordered.extend(members[index] for members in rotation if index < len(members))
    return ordered


def run_concurrently(
    planned: list[tuple[Case, Job]],
    execute: Callable[[Case, Job], dict[str, Any]],
    *,
    concurrency: int,
    per_model: int,
    abort_reason: Callable[[dict[str, Any]], str | None],
) -> tuple[list[dict[str, Any]], str | None]:
    """Run the plan, never holding more than `per_model` runs of one model.

    A worker takes the first waiting run whose model still has room rather than
    a fixed share of the plan, so a model the provider is throttling delays
    only its own remaining runs and the pool keeps working on the others.
    """
    waiting = list(planned)
    results: list[dict[str, Any]] = []
    in_flight: dict[tuple[str, str, str | None], int] = {}
    stalled: collections.Counter[tuple[str, str, str | None]] = collections.Counter()
    aborted: str | None = None
    condition = threading.Condition()

    def limit_for(key: tuple[str, str, str | None]) -> int:
        # The per-model cap exists so we do not throttle a provider that is
        # answering. A provider that answers nothing is not being throttled by
        # us, and its runs only sit out the silence budget, so let them sit it
        # out together. Measured: 20 dead runs held 56% of the container time of
        # a matrix whose real work took 1.2 of 3.0 hours.
        return len(planned) if stalled[key] >= STALLS_BEFORE_UNCAPPED else per_model

    def take() -> tuple[Case, Job] | None:
        with condition:
            while True:
                if aborted is not None or not waiting:
                    return None
                for index, pair in enumerate(waiting):
                    key = model_key(pair[1])
                    if in_flight.get(key, 0) < limit_for(key):
                        in_flight[key] = in_flight.get(key, 0) + 1
                        del waiting[index]
                        return pair
                # Everything left belongs to a model that is already at its
                # limit, so wait for one of those to finish rather than start it.
                condition.wait()

    def worker() -> None:
        nonlocal aborted
        while True:
            pair = take()
            if pair is None:
                return
            case, job = pair
            result = None
            try:
                result = execute(case, job)
            finally:
                with condition:
                    key = model_key(job)
                    in_flight[key] -= 1
                    if result is not None and result.get("status") == "error" and "no output" in str(result.get("failure") or ""):
                        stalled[key] += 1
                    condition.notify_all()
            with condition:
                results.append(result)
                if aborted is None and (reason := abort_reason(result)) is not None:
                    aborted = reason
                    print(f"ERROR: {reason}; stopping the matrix", file=sys.stderr)
                condition.notify_all()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        workers = [pool.submit(worker) for _ in range(min(concurrency, len(planned)))]
    for finished in workers:
        finished.result()
    return results, aborted


def run_matrix(args: argparse.Namespace) -> int:
    matrix = load_matrix(args.matrix)
    if args.image:
        matrix = dataclasses.replace(matrix, image=args.image)
    if args.concurrency < 1:
        raise RuntimeError("concurrency must be at least 1")
    if args.per_model_concurrency < 1:
        raise RuntimeError("per-model concurrency must be at least 1")
    host_source_root = Path(args.source_root).resolve()
    source_gates(host_source_root, matrix.target)
    docker = shutil.which(args.docker) or args.docker

    with contextlib.ExitStack() as stack:
        source_root: Path | None = None
        if matrix.target.mode == "local":
            temporary_root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="agentic-hil-eval-source-")))
            source_root = create_source_snapshot(host_source_root, temporary_root / "source")

        if args.dry_run:
            print(json.dumps(dry_run_plan(matrix, source_root, host_source_root, docker), indent=2, sort_keys=True))
            return 0
        if shutil.which(args.docker) is None:
            raise RuntimeError(f"Docker CLI not found: {args.docker}")
        for job in matrix.jobs:
            ensure_auth(job)
        # A login that can no longer be refreshed fails every run in the matrix,
        # so refuse before spending model budget on it.
        for kind, path in sorted({pair for job in matrix.jobs for pair in resolved_credential_files(job)}):
            state, detail = credential_health(kind, path)
            if state == "expired":
                raise RuntimeError(f"the {kind} login is unusable: {detail}. Sign in again, then rerun.")
            if state == "stale" and not args.refresh_login:
                raise RuntimeError(
                    f"the {kind} login would be refreshed inside the container: {detail}. Refreshing it "
                    f"there can rotate the token and leave this machine's copy rejected. Start the agent "
                    f"CLI once on this machine so it refreshes here, or pass --refresh-login to write the "
                    f"refreshed token back, then rerun."
                )
            print(
                f"WARNING: {kind} is a stored interactive login. Refreshing it inside the container can "
                f"rotate the token and invalidate this machine's copy; an API key or a token minted for "
                f"automation avoids that. State: {detail}",
                file=sys.stderr,
            )

        image_id = image_digest(docker, matrix.image)
        if image_id is None:
            raise RuntimeError(f"Docker image not found: {matrix.image}; run the build command first")
        output_root = Path(args.output).resolve()
        output_root.mkdir(parents=True, exist_ok=False)

        def execute(case: Case, job: Job) -> dict[str, Any]:
            return run_one(
                docker=docker,
                matrix=matrix,
                case=case,
                job=job,
                source_root=source_root,
                host_source_root=host_source_root,
                output_root=output_root,
                image_id=image_id,
                refresh_login=args.refresh_login,
            )

        def authentication_abort(result: dict[str, Any]) -> str | None:
            # Every later run would fail the same way and cost the same.
            if result["status"] == "error" and "could not authenticate" in (result.get("failure") or ""):
                return str(result["failure"])
            return None

        planned = [(case, job) for case in matrix.cases for job in matrix.jobs]
        results = []
        aborted: str | None = None
        if args.concurrency == 1:
            for case, job in planned:
                result = execute(case, job)
                results.append(result)
                if aborted := authentication_abort(result):
                    print(f"ERROR: {aborted}; stopping the matrix", file=sys.stderr)
                    break
        else:
            # Each run owns its containers, its volumes and its output directory,
            # and the source snapshot is mounted read-only, so runs do not share
            # anything a container could write.
            results, aborted = run_concurrently(
                interleaved_plan(planned),
                execute,
                concurrency=args.concurrency,
                per_model=args.per_model_concurrency,
                abort_reason=authentication_abort,
            )
            # Completion order is arbitrary; the report must not be.
            results.sort(key=lambda item: str(item.get("id", "")))

        # Every run gets its own container and its own pair of volumes, so a
        # repetition never inherits state from the one before it.
        cases_by_id = {case.id: case for case in matrix.cases}
        # Keyed by effort too: an escalation re-run must repeat the level the
        # disagreeing repetitions actually ran at, not another level of the
        # same model.
        templates: dict[tuple[str, str, str | None], Job] = {}
        for job in matrix.jobs:
            templates.setdefault((job.agent, job.model, job.reasoning_effort), job)
        while not aborted:
            pending = [
                group
                for group in unstable_groups(results)
                if sum(1 for result in results if result_group(result) == group) < matrix.max_repetitions
            ]
            if not pending:
                break
            # One extra run per disagreeing group, and the whole round together:
            # running these one at a time ignored --concurrency entirely, and a
            # measured matrix spent 124 minutes on 6.6 hours of container work
            # that twelve workers should have finished in about 35.
            round_plan: list[tuple[Case, Job]] = []
            for group in pending:
                case_id, agent, model, effort = group
                done = sum(1 for result in results if result_group(result) == group)
                print(
                    f"repetitions disagreed for {case_id} | {agent} {model} ({effort}) after {done}; running another",
                    file=sys.stderr,
                )
                round_plan.append(
                    (
                        cases_by_id[case_id],
                        dataclasses.replace(
                            templates[(agent, model, None if effort == "default" else effort)],
                            repetition=done + 1,
                        ),
                    )
                )
            if args.concurrency == 1:
                for case, job in round_plan:
                    results.append(execute(case, job))
                    if aborted := authentication_abort(results[-1]):
                        print(f"ERROR: {aborted}; stopping the matrix", file=sys.stderr)
                        break
            else:
                extra, aborted = run_concurrently(
                    interleaved_plan(round_plan),
                    execute,
                    concurrency=args.concurrency,
                    per_model=args.per_model_concurrency,
                    abort_reason=authentication_abort,
                )
                results.extend(sorted(extra, key=lambda item: str(item.get("id", ""))))
        summary = {
            "schema_version": 1,
            "kind": "install-matrix",
            "executor": "docker",
            "status": "passed" if all(result["status"] == "passed" for result in results) else "failed",
            "total": len(results),
            "passed": sum(result["status"] == "passed" for result in results),
            "failed": sum(result["status"] == "failed" for result in results),
            "errors": sum(result["status"] == "error" for result in results),
            "aborted": aborted,
            "unstable": [
                {"case": case_id, "agent": agent, "model": model, "reasoning_effort": effort}
                for case_id, agent, model, effort in unstable_groups(results)
            ],
            # Escalation reruns a disagreeing combination, so the cells are not
            # all the same size. Carried here so a downstream table can weight
            # them instead of averaging uneven cells into one number.
            "repetitions": [
                {
                    "case": case_id,
                    "agent": agent,
                    "model": model,
                    "reasoning_effort": effort,
                    "runs": runs,
                }
                for (case_id, agent, model, effort), runs in sorted(group_counts(results).items())
            ],
            "results": [{"id": result["id"], "status": result["status"]} for result in results],
        }
        (output_root / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if summary["status"] == "passed" else 1


def repin_agent_clis(specifiers: list[str]) -> None:
    """Move the npm lock file to the requested agent CLI versions.

    The image installs the CLIs with "npm ci", which installs exactly what the
    lock file records and refuses anything whose integrity hash does not match.
    That is the property that makes an image reproducible, so a version override
    has to move the lock rather than go around it. The pinned version becomes a
    committed fact instead of a build flag nobody can reconstruct afterwards.
    """
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError(
            "npm is required to override agent CLI versions because the image installs them with "
            f"'npm ci' from a committed lock file; install npm, or edit {CONTAINER_DIRECTORY / 'package.json'} "
            "and regenerate package-lock.json by hand"
        )
    completed = subprocess.run(
        [npm, "install", "--package-lock-only", "--no-audit", "--no-fund", *specifiers],
        cwd=str(CONTAINER_DIRECTORY),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"npm could not pin {', '.join(specifiers)}")
    print(f"repinned {', '.join(specifiers)} in {CONTAINER_DIRECTORY}")


def build_image(args: argparse.Namespace) -> int:
    docker = shutil.which(args.docker)
    if docker is None:
        raise RuntimeError(f"Docker CLI not found: {args.docker}")
    specifiers = [
        f"{AGENT_CLI_PACKAGES[agent]}@{version}"
        for agent, version in (
            ("codex", args.codex_version),
            ("claude-code", args.claude_version),
            ("opencode", args.opencode_version),
        )
        if version
    ]
    if specifiers:
        repin_agent_clis(specifiers)
    command = [
        docker,
        "build",
        "--file",
        str(DEFAULT_DOCKERFILE),
        "--tag",
        args.image,
        str(REPOSITORY_ROOT),
    ]
    return subprocess.run(
        command,
        check=False,
        env=docker_subprocess_environment(docker),
    ).returncode


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Agentic HIL LLM installation evaluation")
    subparsers = root.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="build pinned agent CLI image")
    build.add_argument("--docker", default="docker")
    build.add_argument("--image", default="agentic-hil-install-eval:local")
    build.add_argument("--codex-version")
    build.add_argument("--claude-version")
    build.add_argument("--opencode-version")
    build.set_defaults(function=build_image)

    run_parser = subparsers.add_parser("run", help="run agent CLI x model x case matrix")
    run_parser.add_argument("--docker", default="docker")
    run_parser.add_argument("--matrix", default=str(REPOSITORY_ROOT / "evals" / "install" / "matrix.example.json"))
    run_parser.add_argument("--image")
    run_parser.add_argument("--source-root", default=str(REPOSITORY_ROOT))
    run_parser.add_argument("--output", required=True)
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="how many runs may be in flight at once; each one is capped at 2 CPUs and 2 GB",
    )
    run_parser.add_argument(
        "--per-model-concurrency",
        type=int,
        default=2,
        help="how many runs of one model may be in flight at once; providers throttle a burst",
    )
    run_parser.add_argument(
        "--refresh-login",
        action="store_true",
        help="write a token the agent CLI refreshed back over the stored login it came from",
    )
    run_parser.set_defaults(function=run_matrix)

    report_parser = subparsers.add_parser("report", help="summarise a finished evaluation output directory")
    report_parser.add_argument("--output", required=True)
    report_parser.set_defaults(function=report_results)

    routing_parser = subparsers.add_parser(
        "routing",
        help="show how each run answered the firmware question: MCP server, CLI, or neither",
    )
    routing_parser.add_argument("--output", required=True)
    routing_parser.set_defaults(function=routing_results)

    timings_parser = subparsers.add_parser(
        "timings",
        help="what each agent and model cost, to start the slowest first next time",
    )
    timings_parser.add_argument("--output", required=True)
    timings_parser.set_defaults(function=timings_results)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.function(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
