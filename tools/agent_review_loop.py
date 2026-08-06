"""Drive a Claude Code implement/commit + Codex review loop until the review is clean.

The loop is deliberately asymmetric. Claude Code owns the working tree: it edits,
runs whatever checks the repository defines, and commits. Codex never edits code;
it reads the commits produced in that round, writes a review document, and returns
a machine-readable verdict. The next Claude round is handed that document as its
task. The loop ends when a review reports no findings, or when the configured
number of rounds is exhausted -- whichever comes first.

The two agents do not share a working tree. The reviewer gets a checkout of its
own at the commit under review, so its test runs, caches and temp directories
never land in the tree the implementer is about to commit, and "do not edit the
code" stops being a promise the prompt has to extract. The one path it may write
outside that checkout is the review document the next implementer round reads.

Four further properties make it safe to run unattended:

* Every round must produce at least one commit. A fix round that changes nothing
  is a stalled loop, not progress, so it stops instead of burning the remaining
  rounds on an agent that has given up.
* The verdict is parsed from a JSON schema Codex is required to satisfy, not from
  prose. Prose like "looks good apart from ..." has no stable meaning to a loop
  condition, and guessing at it is how a loop exits one round before the bug.
* A round that cannot finish still commits. An implementer killed at `--timeout`
  leaves a whole round's work uncommitted, because a round commits once and at
  the end; that commit is made here instead, marked in its subject as the
  unfinished thing it is, so the next run starts from it.
* The loop stops when it stops contributing. `--max-rounds` counts rounds and
  cannot tell a round that closed four findings from one that moved four around,
  so `--stop-after-flat-rounds` watches the finding count instead.

The two agents do not have the same reach, either, and the run header says so:
Claude Code has the network and can read a linked issue, while Codex under
`--sandbox workspace-write` cannot. A task that points at a URL is complete for
the agent writing the code and empty for the one checking it against the intent.

Example:

    python tools/agent_review_loop.py \
        --task "Add a --timeout option to agentic-hil probe" \
        --claude-model opus \
        --codex-model gpt-5.1-codex \
        --max-rounds 5

Both agents run non-interactively with permissions pre-granted, so run this only
in a repository you are willing to let them commit to.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO

CLEAN = "clean"
CHANGES_REQUESTED = "changes_requested"

# Codex is pinned to this shape so the loop condition reads a value, not a mood.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": [CLEAN, CHANGES_REQUESTED]},
        "findings": {"type": "integer", "minimum": 0},
        "review_file": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["status", "findings", "review_file", "summary"],
    "additionalProperties": False,
}

# Fallback for a Codex build that ignores --output-schema: the prompt also asks
# for this sentinel, and a loop that cannot read a verdict has to stop.
SENTINEL = re.compile(r"REVIEW_STATUS:\s*(CLEAN|CHANGES_REQUESTED)", re.IGNORECASE)

EXIT_CLEAN = 0
EXIT_ROUNDS_EXHAUSTED = 1
EXIT_STALLED = 2
EXIT_FAILED = 3
EXIT_NO_PROGRESS = 4

# Written into each directory the loop keeps its own paperwork in. `*` covers
# everything in the directory that holds it, this file included.
PAPERWORK_IGNORE = """\
# Written by tools/agent_review_loop.py. Everything here belongs to the review
# loop rather than to this repository: review documents, agent transcripts and
# run summaries. Ignoring it keeps the next run's preflight from refusing a tree
# this run dirtied, and keeps an agent's `git add -A` from committing the last
# run's paperwork along with its work.
*
"""

# How the salvage commit a cut-short round leaves behind is marked. Nothing
# downstream should mistake it for finished work, so it says so in the subject.
SALVAGE_SUBJECT = "wip(review-loop): round {number} was cut short before the implementer committed"

# Codex turns network access on for `workspace-write` through this one setting,
# so a run that has it does not need to be told the reviewer is offline.
NETWORK_ENABLED = re.compile(r"^sandbox_workspace_write\.network_access\s*=\s*(true|1)\s*$", re.IGNORECASE)
# What a task description looks like when it points somewhere instead of saying
# what it means. The reviewer usually cannot follow either of these.
POINTS_OUTWARD = re.compile(r"https?://\S+|\bgh\s+(?:issue|pr)\s+view\b")

# Reviewing commits that already exist needs no task description, but both agents
# still have to be told what to measure the code against.
NO_TASK = (
    "No task was given. These commits already existed when the loop started. Judge them "
    "on their own merits: what the commit messages and the diff set out to do, and whether "
    "the code does it correctly."
)

# Refs a branch may have forked from. All of them are tried, and disagreement
# between them is reported rather than resolved by order: a repository holding
# both 'main' and 'master' has been seen to make first-match pick the wrong one.
MAIN_BRANCH_CANDIDATES = ("origin/HEAD", "origin/main", "origin/master", "main", "master")

# git clone --local hardlinks the object store, and the only failure that a
# second attempt without hardlinks can fix is the link itself failing. Anything
# else -- a destination git refused, a repository it could not read -- must be
# reported rather than retried, because the retry deletes what it is about to
# clone into.
HARDLINK_REFUSED = re.compile(r"cross-device|failed to create link", re.IGNORECASE)


@dataclass
class Verdict:
    status: str
    findings: int
    review_file: str
    summary: str

    @property
    def is_clean(self) -> bool:
        return self.status == CLEAN


@dataclass
class RoundRecord:
    number: int
    commits: list[str] = field(default_factory=list)
    review_file: str | None = None
    status: str | None = None
    findings: int | None = None
    summary: str | None = None
    # Set when the round ended in a commit the loop made itself because the
    # implementer never got that far. A reader of run.json has to be able to tell
    # that commit from one an agent stood behind.
    salvaged: str | None = None


class AgentError(RuntimeError):
    """An agent invocation failed, timed out, or produced no usable answer."""


class AgentTimeout(AgentError):
    """The agent was still working when --timeout expired and was killed.

    Worth its own type: an agent killed mid-edit has a working tree full of
    unfinished work, which is a different situation from one that exited on its
    own and a different sentence in the commit that salvages it.
    """


class _Done(Exception):
    """Internal signal: the run finished before reaching the implement loop."""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise AgentError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


@dataclass
class Progress:
    """What the draining thread has seen, for the heartbeat to report."""

    lines: int = 0
    last: float | None = None  # time.monotonic() of the most recent line

    def describe(self) -> str:
        if self.last is None:
            return "no output yet"
        return f"{self.lines} lines, last {time.monotonic() - self.last:.0f}s ago"


def _pump(stream: IO[str], prefix: str, log_handle: IO[str], progress: Progress) -> None:
    """Drain the agent's output. Never stop draining, whatever a line contains.

    This loop is the only thing emptying the child's stdout pipe. An exception
    here does not merely lose a line: the pipe fills, the agent blocks on its
    next write, and the run dies at the timeout with nothing to show for it.
    That is not hypothetical -- one smart quote in a Codex message raised
    UnicodeEncodeError on a cp1252 console and deadlocked a whole round.
    """
    for line in stream:
        progress.lines += 1
        progress.last = time.monotonic()
        with contextlib.suppress(OSError, UnicodeError):
            log_handle.write(line)
            log_handle.flush()
        try:
            print(f"{prefix} {line.rstrip()}", flush=True)
        except (OSError, UnicodeError):
            safe = line.rstrip().encode("ascii", "replace").decode("ascii")
            with contextlib.suppress(OSError):
                print(f"{prefix} {safe}", flush=True)


def _terminate_tree(process: subprocess.Popen[str]) -> None:
    """Kill the agent and everything it spawned.

    An agent that hit the timeout is usually blocked inside a child shell it
    started. Killing only the agent leaves that shell holding the working tree.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            text=True,
        )
    process.kill()


def run_agent(
    command: list[str],
    prompt: str,
    cwd: Path,
    prefix: str,
    log_path: Path,
    timeout: float,
    dry_run: bool,
    env: dict[str, str] | None = None,
    heartbeat: float = 0.0,
) -> None:
    """Run an agent CLI with the prompt on stdin, mirroring output to console and log.

    The prompt goes over stdin rather than argv because a review document pasted
    into a prompt outgrows the Windows command-line limit long before it outgrows
    anything else.

    `claude -p` prints nothing at all until it has finished, so an implement round
    is silent for the thirty-five to fifty minutes it takes. Silence that long is
    indistinguishable from a hang -- it was read as one twice in a single session
    -- so `heartbeat` seconds of it produces a line saying how long the agent has
    been running and whether it has said anything yet. It is deliberately the
    loop's own line rather than a different `--output-format`: nothing here parses
    what an agent prints, and a machine-readable stream would change what a
    transcript is for the sake of a progress indicator.
    """
    printable = " ".join(command)
    print(f"\n{prefix} $ {printable}", flush=True)
    if dry_run:
        print(f"{prefix} [dry-run] prompt ({len(prompt)} chars) not sent", flush=True)
        return

    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    print(f"{prefix} started; limit {timeout:.0f}s (log: {log_path})", flush=True)
    progress = Progress()
    with log_path.open("w", encoding="utf-8") as log_handle:
        log_handle.write(f"$ {printable}\n\n--- prompt ---\n{prompt}\n--- output ---\n")
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        assert process.stdin is not None and process.stdout is not None
        reader = threading.Thread(target=_pump, args=(process.stdout, prefix, log_handle, progress), daemon=True)
        reader.start()
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except OSError as error:  # the agent exited before reading the prompt
            # It may still be alive and holding the working tree; do not leave it
            # running just because it stopped listening.
            _terminate_tree(process)
            reader.join(timeout=10)
            raise AgentError(f"{prefix} refused the prompt: {error}") from error

        deadline = started + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_tree(process)
                reader.join(timeout=10)
                raise AgentTimeout(f"{prefix} exceeded --timeout of {timeout:.0f}s")
            try:
                returncode = process.wait(timeout=min(heartbeat, remaining) if heartbeat > 0 else remaining)
                break
            except subprocess.TimeoutExpired:
                if heartbeat > 0:
                    elapsed = time.monotonic() - started
                    print(f"{prefix} ... {elapsed:.0f}s of {timeout:.0f}s, {progress.describe()}", flush=True)
        reader.join(timeout=30)

    elapsed = time.monotonic() - started
    print(f"{prefix} exit {returncode} after {elapsed:.0f}s (log: {log_path})", flush=True)
    if returncode != 0:
        raise AgentError(f"{prefix} exited with {returncode}; see {log_path}")


def excluding(repo: Path, paths: tuple[Path, ...]) -> list[str]:
    """Pathspecs adding everything in `repo` except the loop's own directories.

    This repository gitignores `.agentic-loop/`, so `git add -A` leaves the run's
    review documents and transcripts alone here. Nothing guarantees that
    elsewhere, and a salvage commit that carries a review of itself is not the
    round's work. Directories outside the repository are dropped rather than
    named: git rejects a pathspec that leaves the work tree, and refusing the
    whole commit over one would defeat the point of making it.
    """
    pathspecs = ["--", "."]
    for path in paths:
        with contextlib.suppress(ValueError, OSError):
            relative = path.resolve().relative_to(repo.resolve())
            if relative != Path("."):
                pathspecs.append(f":!{relative.as_posix()}")
    return pathspecs


def in_work_tree(repo: Path, path: Path) -> str | None:
    """`path` as a pathspec relative to the work tree, or None when it is outside it.

    Outside the work tree git cannot see the path at all, and the repository root
    itself is not a directory this may treat as the loop's own.
    """
    with contextlib.suppress(ValueError, OSError):
        relative = path.resolve().relative_to(repo.resolve()).as_posix()
        return None if relative == "." else relative
    return None


def tracked(repo: Path, relative: str) -> bool:
    """Whether the repository has anything of its own committed under `relative`.

    A directory the operator tracks is the operator's. When git cannot answer,
    the safe reading is that it is theirs.
    """
    try:
        return bool(git(repo, "ls-files", "--", relative))
    except AgentError:
        return True


def paperwork_dir(repo: Path, root: Path, run_id: str) -> Path:
    """Create this run's review or log directory, invisible to the repository it reviews.

    The loop's paperwork lives inside `--repo`, and in a repository that does not
    ignore the path that breaks the loop twice over: the next run's `preflight`
    refuses to start on the tree the last run dirtied, and under `--allow-dirty`
    an agent's own `git add -A` commits the last run's review documents along
    with its work. This repository ignores `.agentic-loop/` and so never showed
    it; the tool exists to be pointed at repositories that do not.

    So the root carries a `.gitignore` of `*`, which git applies to the directory
    holding it -- including that `.gitignore` itself, so there is nothing left to
    report -- and nothing is asked of the repository under review. A repository
    that already ignores the path is undisturbed: git does not descend into an
    ignored directory, so its own rule keeps answering and this file is never
    even read.

    A root with tracked files under it is skipped. `--review-dir` pointed at a
    directory the operator committed is theirs, and `*` there would hide their
    files from git rather than the loop's. Everything else gets one, including a
    root left behind by a run from before this existed, which is the shape every
    repository the loop has already visited is in.

    This does not replace the `excluding()` pathspecs in `salvage_commit`. They
    guard different things: this covers every `git add -A` an agent runs and the
    preflight of the next run, and stands down on a tracked directory; those are
    the loop's own commit, and hold for the reviewer checkout too, which is
    outside the repository by default and never gets a file written into it.
    """
    directory = root / run_id
    directory.mkdir(parents=True, exist_ok=True)
    ignore = root / ".gitignore"
    relative = in_work_tree(repo, root)
    if not ignore.exists() and relative is not None and not tracked(repo, relative):
        ignore.write_text(PAPERWORK_IGNORE, encoding="utf-8")
    return directory


def salvage_commit(repo: Path, number: int, error: AgentError, paperwork: tuple[Path, ...] = ()) -> str | None:
    """Commit what a round left in the tree when the round is not going to finish.

    A round commits once, at the end. An implementer stopped before it got there
    -- killed at `--timeout`, or exited non-zero -- therefore leaves the whole
    round's work uncommitted, and the loop is about to stop and take the record
    of it with it. This is not a hypothesis: measured rounds ran 2861s, 2621s and
    2173s against the default hour, the first of them 79% of the way to being
    killed, and a round that was killed cost work somebody committed by hand
    afterwards.

    So it is committed instead, marked in the subject as the incomplete thing it
    is. Nothing here was reviewed and the round's own checks may never have run;
    an incomplete commit says that plainly and the next run can build on it,
    where a discarded working tree says nothing and cannot be built on at all.

    Returns the one-line description of the commit, or None when there was
    nothing to commit or git refused it. A refusal is reported and not raised:
    this runs while another failure is already on its way out, and replacing that
    failure with this one loses the reason the run stopped.
    """
    try:
        if not git(repo, "status", "--porcelain"):
            return None
        git(repo, "add", "-A", *excluding(repo, paperwork))
        if not git(repo, "diff", "--cached", "--name-only"):
            return None  # nothing but ignored files and this run's own paperwork
        cause = "the agent ran out of time" if isinstance(error, AgentTimeout) else "the agent stopped"
        git(
            repo,
            "commit",
            "-m",
            SALVAGE_SUBJECT.format(number=number),
            "-m",
            f"{cause}, so this is a round's work part-way through rather than a finished change. "
            "Nothing in it has been reviewed and the checks the round would have run may never have run. "
            "It is committed rather than discarded so that the next run starts from it instead of from an "
            "unexplained working tree.\n\n"
            f"Stopped by: {error}",
        )
    except AgentError as refusal:
        print(
            f"warning: could not commit what round {number} left behind ({refusal}). "
            f"The work is still in {repo}; commit it by hand before starting another run.",
            file=sys.stderr,
        )
        return None
    line = git(repo, "log", "--oneline", "-1")
    print(f"\nround {number}: committed the unfinished work as {line}", flush=True)
    return line


def round_scratch(root: Path, number: int, role: str) -> Path:
    """Create one agent's scratch directory for one round, never reusing one.

    pytest deletes an existing `--basetemp` and immediately recreates it. On
    Windows a directory whose deletion is still pending -- one handle left open
    by the previous round is enough -- cannot be recreated, and the round dies
    with `PermissionError: [WinError 5]`. Left to itself pytest also builds a
    hardened `pytest-of-<user>` root, whose inheritance is stripped and whose
    access is granted through OWNER RIGHTS alone, which a differently-tokened
    process cannot use at all. Both were watched happening, repeatedly, in one
    session.

    Setting TMP and TEMP for *this* process does not decide it, because the agent
    writes its own pytest command line. So the directory is made here, handed
    over in the environment, and named in the prompt -- and it is a new one every
    round, so there is never a previous round's directory to trip over.
    """
    directory = root / f"round-{number:02d}-{role}"
    attempt = 0
    while directory.exists():
        attempt += 1
        directory = root / f"round-{number:02d}-{role}-{attempt}"
    directory.mkdir(parents=True)
    return directory


def scratch_block(scratch: Path) -> str:
    """The paragraph that tells an agent where its temporary files go."""
    return f"""
SCRATCH SPACE
{scratch.as_posix()}
That directory was created for this round and nothing has used it before. TMP,
TEMP and TMPDIR already point at it. If you run pytest, give it a subdirectory
that does not exist yet -- `--basetemp={scratch.as_posix()}/pytest-1`, then
`pytest-2` for the next run. Do not choose a base temp directory anywhere else
and never reuse one: pytest deletes an existing `--basetemp` and immediately
recreates it, which fails on Windows while any handle from the previous run is
still open.
"""


def create_review_checkout(mode: str, repo: Path, destination: Path) -> Path | None:
    """Give the reviewer a checkout of its own, separate from the implementer's tree.

    Sharing one working tree makes the two agents contend for it: the reviewer's
    test runs drop caches and temp directories into the tree the implementer is
    about to commit, and nothing but a prompt stops a reviewer from "just fixing"
    what it found. A checkout at the reviewed commit removes both by construction,
    and it pins the review to committed code rather than to whatever the tree
    happens to hold at that moment.

    The cost is real and worth stating: a fresh checkout contains committed files
    only. Virtualenvs, build output, and anything else gitignored are absent, so
    the reviewer cannot reuse the implementer's installed environment.
    """
    if mode == "none":
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    if mode == "clone":
        # The retry below deletes the destination, so nothing this call did not
        # create may be sitting there. --review-checkout-dir is a caller's
        # value: pointed at the repository itself -- and under
        # tools/loop_in_container.py that is a read-write mount of the
        # operator's own checkout -- a clone git safely refuses would otherwise
        # be followed by a delete that does not refuse anything.
        if destination.exists():
            raise AgentError(
                f"the reviewer's checkout directory already exists: {destination}. "
                "Name one that does not, or remove it first."
            )
        # --local hardlinks the object store when both sides share a filesystem,
        # so this stays cheap; a separate object store also means the reviewer
        # cannot damage the implementer's repository.
        try:
            git(repo, "clone", "--local", "--no-checkout", str(repo), str(destination))
        except AgentError as error:
            # A hardlink cannot cross a filesystem boundary, and under
            # tools/loop_in_container.py the repository is a bind mount while
            # this checkout is on the container's own filesystem. git reports
            # "Invalid cross-device link" and stops rather than falling back, so
            # the fallback is here: copy the objects instead of linking them.
            # Every other failure is the caller's to see.
            if not HARDLINK_REFUSED.search(str(error)):
                raise
            shutil.rmtree(destination, ignore_errors=True)
            git(repo, "clone", "--local", "--no-hardlinks", "--no-checkout", str(repo), str(destination))
    elif mode == "worktree":
        git(repo, "worktree", "add", "--detach", str(destination), "HEAD")
    else:
        raise AgentError(f"unknown --review-checkout mode: {mode}")
    return destination


def sync_review_checkout(mode: str, repo: Path, checkout: Path, commit: str) -> None:
    """Move the reviewer's checkout to the commit under review and scrub the rest."""
    if mode == "clone":
        # Fetch HEAD explicitly rather than relying on branch refspecs: the
        # implementer may have committed onto a detached HEAD, and those commits
        # are reachable from no branch a default fetch would bring across.
        git(checkout, "fetch", "--no-tags", "--quiet", str(repo), "+HEAD:refs/agentic-loop/head")
    git(checkout, "checkout", "--detach", "--quiet", commit)
    # Whatever the previous round's review left behind is not part of this round.
    git(checkout, "clean", "-xfdq")


def remove_review_checkout(mode: str, repo: Path, checkout: Path) -> None:
    if mode == "worktree":
        with contextlib.suppress(AgentError):
            git(repo, "worktree", "remove", "--force", str(checkout))
    if checkout.is_dir():
        # Git marks pack files read-only, which stops a plain rmtree on Windows.
        for path in sorted(checkout.rglob("*"), reverse=True):
            with contextlib.suppress(OSError):
                path.chmod(0o700)
        shutil.rmtree(checkout, ignore_errors=True)


def claude_command(options: argparse.Namespace) -> list[str]:
    command = [options.claude_bin, "-p", "--output-format", "text"]
    if options.claude_model:
        command += ["--model", options.claude_model]
    if options.claude_effort:
        command += ["--effort", options.claude_effort]
    command += ["--permission-mode", options.claude_permission_mode]
    if options.claude_allowed_tools:
        command += ["--allowedTools", *options.claude_allowed_tools]
    command += options.claude_arg
    return command


def codex_command(
    options: argparse.Namespace,
    schema_path: Path,
    last_message_path: Path,
    review_dir: Path,
    workspace: Path,
    scratch: Path | None = None,
) -> list[str]:
    command = [options.codex_bin, "exec", "-"]
    if options.codex_model:
        command += ["--model", options.codex_model]
    if options.codex_effort:
        command += ["-c", f"model_reasoning_effort={options.codex_effort}"]
    # The review document is the one thing the reviewer writes outside its own
    # workspace, because it is the one thing the implementer has to read next.
    command += ["--add-dir", str(review_dir)]
    if scratch is not None:
        # Its scratch directory is outside the workspace too, and a sandbox that
        # cannot write there is a reviewer that puts its pytest base temp
        # directory wherever it *can* write -- which was the review directory,
        # beside the document it was writing.
        command += ["--add-dir", str(scratch)]
    command += ["--output-schema", str(schema_path), "--output-last-message", str(last_message_path)]
    command += ["--color", "never"]
    command += options.codex_arg
    # Last, after everything the caller passed through, because Codex takes the
    # final occurrence of either. `--codex-arg=--cd=/repo` is otherwise a
    # reviewer working in the implementer's tree -- under
    # tools/loop_in_container.py, the read-write repository mount -- and
    # `--codex-arg=--sandbox=...` a sandbox nobody chose. Naming the working
    # root rather than relying on the process's cwd is what makes the ordering
    # mean something.
    command += ["--cd", str(workspace)]
    command += ["--sandbox", options.codex_sandbox]
    return command


def implement_prompt(task: str, review_dir: Path, branch: str, scratch: Path) -> str:
    return f"""You are the implementer in an automated implement-then-review loop.

Repository branch: {branch}

TASK
{task}
{scratch_block(scratch)}
RULES
1. Implement the task in this repository. Follow the repository's own conventions
   and the instructions in AGENTS.md / CLAUDE.md if present.
2. Run the repository's lint and test commands for the code you touched, and fix
   what you break.
3. Commit your work with `git commit`. One or more commits is fine; at least one
   commit is mandatory. Do not push, do not amend commits that already existed,
   do not create branches, do not open pull requests.
4. Do not write anything into {review_dir.as_posix()} -- that directory belongs to
   the reviewer.
5. Finish by printing a short summary of what you changed and which checks you ran.
"""


def fix_prompt(task: str, review_path: Path, round_number: int, branch: str, scratch: Path) -> str:
    return f"""You are the implementer in an automated implement-then-review loop.
This is round {round_number}. An independent reviewer read your previous commits
and wrote findings to a review document.

Repository branch: {branch}

ORIGINAL TASK
{task}

REVIEW DOCUMENT
{review_path.as_posix()}
{scratch_block(scratch)}
RULES
1. Read the review document first. Address every finding in it.
2. If a finding is wrong or not worth acting on, do not silently skip it: say so
   explicitly in your final summary with your reasoning, so the next review round
   sees the disagreement.
3. Run the repository's lint and test commands for the code you touched.
4. Commit your work with `git commit`. At least one commit is mandatory. Do not
   push, do not amend existing commits, do not create branches.
5. Do not edit the review document and do not write into its directory.
6. Finish by printing a short summary: finding addressed, how, and anything you
   deliberately rejected.
"""


def review_prompt(
    task: str,
    review_path: Path,
    diff_range: str,
    round_number: int,
    previous_review: Path | None,
    isolated: bool,
    scratch: Path,
    has_network: bool,
) -> str:
    workspace = ""
    if isolated:
        workspace = """
YOUR WORKSPACE
You are in a checkout of your own, at the commit under review. The implementer's
working tree is elsewhere and you cannot reach it -- that is deliberate, and it
means nothing you do here can disturb the code being written. It also means this
checkout holds committed files only: virtualenvs, build output and anything else
gitignored are absent. Install what you need here if you want to run something,
and say so in the review if you could not run it.

The places you can write are this checkout, the scratch directory named below,
and the directory the review document goes in. Nowhere else.

If the project's tests will not run, do not report that as a defect in the code
unless you have established that the code is why. Say plainly in the review what
you could not run and what you checked instead.
"""
    network = ""
    if not has_network:
        network = """
NO NETWORK
You have no network access: you cannot open a URL, fetch an issue, or run
`gh issue view`. The implementer could. If the task above points at something
rather than stating it, you cannot read what it points at -- judge the code
against the text you were given and say in the review which part of the intent
you could not check, rather than assuming it.
"""
    history = ""
    if previous_review is not None:
        history = f"""
PREVIOUS REVIEW
{previous_review.as_posix()}
Read it. Verify each earlier finding was actually addressed. A finding that came
back, or was answered with a change that does not fix it, is still a finding.
"""
    return f"""You are the reviewer in an automated implement-then-review loop. This is
review round {round_number}. You review code. You never edit code, never commit,
never revert, and never run the implementer's task yourself.

ORIGINAL TASK
{task}
{workspace}{network}{scratch_block(scratch)}
WHAT TO REVIEW
The commits in `{diff_range}`. Start with `git log --stat {diff_range}` and
`git diff {diff_range}`. Read the surrounding files when the diff alone is not
enough to judge correctness.
{history}
WHAT COUNTS AS A FINDING
Correctness bugs, broken or missing error handling, security problems, data loss,
API/behaviour regressions, tests that do not test what they claim, and violations
of the repository's documented conventions. Do not report pure style preferences,
and do not invent work outside the original task.

OUTPUT 1 -- the review document
Write your review to exactly this path: {review_path.as_posix()}
Create the parent directory if it does not exist. Use this structure:

    # Review round {round_number}
    - Range: {diff_range}
    - Verdict: CLEAN or CHANGES REQUESTED

    ## Findings
    ### <N>. <short title> (severity: blocker|major|minor)
    - File: path:line
    - Problem: ...
    - Suggested fix: ...

    ## Verified from previous round
    (only for round 2 and later)

If you have no findings, still write the document with an empty Findings section
and state what you checked and why it is sound.

OUTPUT 2 -- the verdict
Your final message must be a single JSON object and nothing else:

    {{"status": "clean" | "changes_requested",
      "findings": <integer count of findings>,
      "review_file": "{review_path.as_posix()}",
      "summary": "<one or two sentences>"}}

Use "clean" only when findings is 0. Also include the line
REVIEW_STATUS: CLEAN or REVIEW_STATUS: CHANGES_REQUESTED inside the review
document so the verdict survives even if the JSON is lost.
"""


def parse_verdict(last_message_path: Path, review_path: Path) -> Verdict:
    text = last_message_path.read_text(encoding="utf-8") if last_message_path.is_file() else ""
    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("status") in {CLEAN, CHANGES_REQUESTED}:
            return Verdict(
                status=str(payload["status"]),
                findings=int(payload.get("findings", 0)),
                review_file=str(payload.get("review_file", review_path.as_posix())),
                summary=str(payload.get("summary", "")).strip(),
            )

    for source in (text, review_path.read_text(encoding="utf-8") if review_path.is_file() else ""):
        match = SENTINEL.search(source)
        if match:
            status = CLEAN if match.group(1).lower() == "clean" else CHANGES_REQUESTED
            return Verdict(
                status=status,
                findings=0 if status == CLEAN else -1,
                review_file=review_path.as_posix(),
                summary="parsed from REVIEW_STATUS sentinel; no structured verdict was returned",
            )

    raise AgentError(
        f"the reviewer returned no readable verdict (checked {last_message_path} and {review_path}); "
        "stopping rather than guessing whether the review was clean"
    )


def corroborate(verdict: Verdict, review_path: Path) -> Verdict:
    """Accept "clean" only when the review document backs it up.

    "Clean" is the value that ends the loop, so it is the one worth doubting.
    Codex emits intermediate messages in the requested schema, not just the final
    one -- an observed run announced status "clean" with an empty review_file
    before it had read a single line of the diff. The final message is the one
    read here, but the same failure at the end of a run would retire an unfinished
    review as a pass. A clean verdict therefore has to agree with the document it
    claims to have written.
    """
    if not verdict.is_clean:
        return verdict

    if not review_path.is_file():
        raise AgentError(
            f"the reviewer reported '{CLEAN}' but wrote no review document at {review_path}; "
            "refusing to end the loop on an unverifiable pass"
        )

    document = review_path.read_text(encoding="utf-8", errors="replace")
    match = SENTINEL.search(document)
    if match is not None and match.group(1).lower() != CLEAN:
        stated = f"{review_path.name} says {match.group(1)}"
    elif verdict.findings > 0:
        stated = f"the verdict itself reports findings={verdict.findings}"
    else:
        return verdict

    print(
        f"warning: the reviewer's verdict said '{CLEAN}' but {stated}; treating the round as changes_requested",
        file=sys.stderr,
    )
    return Verdict(
        status=CHANGES_REQUESTED,
        findings=max(verdict.findings, 1),
        review_file=verdict.review_file,
        summary=f"downgraded from clean: {stated}",
    )


def _json_candidates(text: str) -> list[str]:
    """Yield the whole message first, then any fenced or trailing JSON object."""
    stripped = text.strip()
    candidates = [stripped] if stripped else []
    candidates += re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    last_open = text.rfind("{")
    last_close = text.rfind("}")
    if last_open != -1 and last_close > last_open:
        candidates.append(text[last_open : last_close + 1])
    return candidates


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Loop Claude Code (implement + commit) against Codex (review) until the review is clean.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    task_group = parser.add_mutually_exclusive_group()
    task_group.add_argument("--task", help="The task Claude Code should implement. Required unless --start-with review.")
    task_group.add_argument("--task-file", type=Path, help="Read the task from this file instead of --task.")

    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Repository to work in.")
    parser.add_argument(
        "--start-with",
        default="implement",
        choices=["implement", "review"],
        help=(
            "'implement' writes code first, then reviews it. 'review' reviews commits that already exist "
            "and only then hands them to the implementer."
        ),
    )
    parser.add_argument(
        "--review-base",
        default=None,
        metavar="REF",
        help=(
            "With --start-with review: review REF..HEAD. Defaults to the merge base with the repository's "
            "main branch."
        ),
    )
    parser.add_argument(
        "--review-range",
        default=None,
        metavar="RANGE",
        help="With --start-with review: review this git range verbatim, e.g. 'abc123..def456'. Overrides --review-base.",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=5,
        help="Maximum implement+review rounds. 0 with --start-with review means review once and stop.",
    )
    parser.add_argument(
        "--review-dir",
        type=Path,
        default=Path(".agentic-loop/reviews"),
        help="Directory for review documents, relative to --repo unless absolute.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path(".agentic-loop/logs"),
        help="Directory for agent transcripts, relative to --repo unless absolute.",
    )

    parser.add_argument("--claude-bin", default="claude", help="Claude Code executable.")
    parser.add_argument("--claude-model", default=None, help="Model for Claude Code, e.g. opus or sonnet.")
    parser.add_argument(
        "--claude-effort",
        default=None,
        # Passed through verbatim. Pinning the accepted set here would mean this
        # script has to be edited before a newly shipped effort level can be used,
        # and the CLI rejects a bad value on its own anyway.
        metavar="LEVEL",
        help="Reasoning effort for Claude Code, passed through to --effort.",
    )
    parser.add_argument(
        "--claude-permission-mode",
        default="acceptEdits",
        choices=["acceptEdits", "auto", "bypassPermissions", "dontAsk"],
        help="Claude Code permission mode; the loop is unattended, so no mode that prompts is offered.",
    )
    parser.add_argument(
        "--claude-allowed-tools",
        nargs="*",
        default=["Bash", "Edit", "Write", "Read", "Glob", "Grep"],
        help="Tools granted to Claude Code. Bash is required for git commit.",
    )
    parser.add_argument(
        "--claude-arg",
        action="append",
        # Not default=[]: argparse appends into the default object itself, which
        # leaks arguments between calls when this module is driven from a test.
        default=None,
        help="Extra argument passed through to the Claude Code CLI (repeatable).",
    )

    parser.add_argument("--codex-bin", default="codex", help="Codex executable.")
    parser.add_argument("--codex-model", default=None, help="Model for Codex, e.g. gpt-5.1-codex.")
    parser.add_argument(
        "--codex-effort",
        "--codex-reasoning-effort",
        dest="codex_effort",
        default=None,
        metavar="LEVEL",
        help="Codex reasoning effort, passed through as -c model_reasoning_effort.",
    )
    parser.add_argument(
        "--review-checkout",
        default="clone",
        choices=["clone", "worktree", "none"],
        help=(
            "How the reviewer gets its own copy of the code. 'clone' is a separate repository, "
            "'worktree' shares the object store, 'none' reviews in the implementer's tree."
        ),
    )
    parser.add_argument(
        "--review-checkout-dir",
        type=Path,
        default=None,
        help="Where to put the reviewer's checkout. Default: a run-specific directory under the system temp dir.",
    )
    parser.add_argument(
        "--keep-review-checkout",
        action="store_true",
        help="Leave the reviewer's checkout in place after the run instead of deleting it.",
    )
    parser.add_argument(
        "--codex-sandbox",
        default="workspace-write",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="Codex sandbox. workspace-write is the minimum that lets it write the review document.",
    )
    parser.add_argument(
        "--codex-arg",
        action="append",
        default=None,
        help="Extra argument passed through to the Codex CLI (repeatable).",
    )

    parser.add_argument("--timeout", type=float, default=3600.0, help="Seconds allowed per agent invocation.")
    parser.add_argument(
        "--heartbeat",
        type=float,
        default=60.0,
        help="Seconds of silence before printing how long the agent has been running. 0 prints nothing.",
    )
    parser.add_argument(
        "--stop-after-flat-rounds",
        type=int,
        default=2,
        # Two, not one: a single round that finds the same number is weak
        # evidence, because a fix legitimately exposes something new. Two
        # consecutive is the pattern that was actually observed at the end of a
        # run (10, 9, 4, 4 and 8, 7, 6), where the closing rounds moved the
        # remainder around at roughly an hour each. See --help for the rest.
        help=(
            "Stop when this many consecutive review rounds fail to get the finding count below the fewest "
            "seen so far. 0 keeps going until --max-rounds."
        ),
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Start even if the working tree has uncommitted changes; they will be swept into agent commits.",
    )
    parser.add_argument(
        "--continue-on-no-commit",
        action="store_true",
        help="Keep looping when an implementation round produces no commit instead of stopping as stalled.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the agent commands without running them.")
    options = parser.parse_args(argv)
    options.claude_arg = options.claude_arg or []
    options.codex_arg = options.codex_arg or []
    if options.start_with == "implement":
        if not (options.task or options.task_file):
            parser.error("--task or --task-file is required unless --start-with review")
        if options.max_rounds < 1:
            parser.error("--max-rounds must be at least 1 when starting with the implementer")
        for name in ("--review-base", "--review-range"):
            if getattr(options, name[2:].replace("-", "_")):
                parser.error(f"{name} only applies to --start-with review")
    elif options.max_rounds < 0:
        parser.error("--max-rounds cannot be negative")
    if options.stop_after_flat_rounds < 0:
        parser.error("--stop-after-flat-rounds cannot be negative")
    if options.heartbeat < 0:
        parser.error("--heartbeat cannot be negative")
    return options


def reviewer_network(sandbox: str, codex_arguments: list[str]) -> bool:
    """Whether the reviewer will be able to reach the network, read off its sandbox.

    This is worth answering rather than assuming, because the two agents do not
    have the same reach and it is the reviewer -- the one checking the work
    against the intent -- that is cut off. Claude Code runs with the network and
    can read a linked issue; Codex under `--sandbox workspace-write` reports the
    call blocked by its proxy. A task that points at a URL is therefore complete
    for one agent and empty for the other.

    Codex denies the network under `read-only` and `workspace-write` and allows
    it under `danger-full-access`; the one setting that moves it is read here in
    every spelling Codex accepts for a config override. Nothing about the host's
    own connectivity is claimed -- a machine that is offline anyway is offline
    for both agents, and this cannot see that.
    """
    if sandbox == "danger-full-access":
        return True
    if sandbox != "workspace-write":
        return False
    expects_setting = False
    for argument in codex_arguments:
        setting = None
        if expects_setting:
            expects_setting, setting = False, argument
        elif argument in ("-c", "--config"):
            expects_setting = True
        elif argument.startswith("--config="):
            setting = argument[len("--config=") :]
        elif argument.startswith("-c") and len(argument) > 2:
            setting = argument[2:].lstrip("=")
        if setting is not None and NETWORK_ENABLED.match(setting):
            return True
    return False


def agent_env(scratch: Path, checkout: Path | None = None) -> dict[str, str]:
    """The environment one agent runs a round in: its own scratch, its own code.

    The scratch directory is this round's and no other round's, which is what
    stops the reviewer inheriting a temporary directory a previous round left
    delete-pending or hardened. Handing it over here is half the job -- an agent
    that writes its own pytest command line ignores TMP and TEMP when it passes
    `--basetemp` -- so `scratch_block` names the same directory in the prompt.
    PYTEST_DEBUG_TEMPROOT is set as well: it is what decides where pytest builds
    its own `pytest-of-<user>` root when nobody passes `--basetemp` at all, and
    that root is the one whose permissions are unusable from another process.
    """
    env = dict(os.environ)
    env.update({key: str(scratch) for key in ("TMP", "TEMP", "TMPDIR")})
    env["PYTEST_DEBUG_TEMPROOT"] = str(scratch)
    if checkout is None:
        return env
    source = checkout / "src"
    if source.is_dir():
        # A review is of one commit, so the reviewer's tests have to import that
        # commit's code. Without this they import whatever an installed copy
        # resolves to, which is the implementer's working tree -- possibly
        # dirty, possibly mid-edit, and under tools/loop_in_container.py on the
        # far side of a bind mount. PYTHONPATH comes ahead of everything
        # site-packages adds, so it decides this rather than argues with it.
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join([str(source), *([existing] if existing else [])])
    return env


@dataclass
class ReviewSetup:
    """Everything a review round needs that does not change between rounds."""

    options: argparse.Namespace
    repo: Path
    checkout: Path | None
    task: str
    review_dir: Path
    log_dir: Path
    schema_path: Path
    scratch_root: Path
    has_network: bool


def perform_review(
    setup: ReviewSetup,
    record: RoundRecord,
    number: int,
    diff_range: str,
    previous_review: Path | None,
    commit: str,
) -> tuple[Verdict, Path] | None:
    """Run one review round. Returns None under --dry-run, where there is no verdict."""
    options = setup.options
    review_path = setup.review_dir / f"round-{number:02d}.md"
    last_message_path = setup.log_dir / f"round-{number:02d}-verdict.txt"
    if setup.checkout is not None:
        sync_review_checkout(options.review_checkout, setup.repo, setup.checkout, commit)
    workspace = setup.checkout or setup.repo
    scratch = round_scratch(setup.scratch_root, number, "codex")
    run_agent(
        codex_command(options, setup.schema_path, last_message_path, setup.review_dir, workspace, scratch),
        review_prompt(
            setup.task,
            review_path,
            diff_range,
            number,
            previous_review,
            setup.checkout is not None,
            scratch,
            setup.has_network,
        ),
        workspace,
        f"[codex  r{number}]",
        setup.log_dir / f"round-{number:02d}-codex.log",
        options.timeout,
        options.dry_run,
        env=agent_env(scratch, setup.checkout),
        heartbeat=options.heartbeat,
    )
    if options.dry_run:
        return None
    verdict = corroborate(parse_verdict(last_message_path, review_path), review_path)
    record.review_file = str(review_path)
    record.status = verdict.status
    record.findings = verdict.findings
    record.summary = verdict.summary
    if not review_path.is_file():
        print(f"warning: the reviewer did not write {review_path}", file=sys.stderr)

    findings = "unknown" if verdict.findings < 0 else str(verdict.findings)
    print(f"\nround {number} verdict: {verdict.status} ({findings} findings)")
    if verdict.summary:
        print(f"  {verdict.summary}")
    print(f"  review: {review_path}")
    return verdict, review_path


def resolve_initial_range(repo: Path, options: argparse.Namespace, head: str) -> str:
    """Work out what a review-first run should look at.

    An explicit range wins, then an explicit base. Failing both, guess where this
    branch forked from -- and if that guess would produce an empty range, say so
    instead of handing the reviewer nothing and calling the result clean.
    """
    if options.review_range:
        return options.review_range
    if options.review_base:
        base = git(repo, "rev-parse", "--verify", f"{options.review_base}^{{commit}}")
        if base == head:
            raise AgentError(f"--review-base {options.review_base} is HEAD; there would be nothing to review")
        return f"{base}..{head}"

    forks: dict[str, list[str]] = {}
    for candidate in MAIN_BRANCH_CANDIDATES:
        try:
            reference = git(repo, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
            base = git(repo, "merge-base", reference, head)
        except AgentError:
            continue  # the ref does not exist here, or shares no history with HEAD
        if base != head:  # HEAD is not ahead of this one, so it defines no range
            forks.setdefault(base, []).append(candidate)

    if len(forks) == 1:
        base, names = next(iter(forks.items()))
        count = git(repo, "rev-list", "--count", f"{base}..{head}")
        print(f"review base : {names[0]} (merge base {base[:12]}, {count} commits)")
        return f"{base}..{head}"

    if not forks:
        raise AgentError(
            "HEAD is not ahead of any of "
            f"{', '.join(MAIN_BRANCH_CANDIDATES)}, so there is no range to review. "
            "Pass --review-base REF or --review-range RANGE."
        )

    # Several plausible bases that disagree -- a repository holding both 'main'
    # and 'master', say. Guessing here is how a one-commit review silently
    # becomes a fifty-commit one, so the caller has to say which it meant.
    detail = ", ".join(
        f"{'/'.join(names)} -> {git(repo, 'rev-list', '--count', f'{base}..{head}')} commits"
        for base, names in forks.items()
    )
    raise AgentError(
        f"cannot tell what to review: HEAD forks from several candidate branches ({detail}). "
        "Pass --review-base REF or --review-range RANGE."
    )


def resolve_executable(name: str, label: str) -> str:
    found = shutil.which(name)
    if not found:
        raise AgentError(f"{label} executable not found on PATH: {name}")
    return found


def preflight(options: argparse.Namespace) -> tuple[Path, str, str]:
    repo = git(options.repo, "rev-parse", "--show-toplevel")
    repo_path = Path(repo).resolve()
    try:
        head = git(repo_path, "rev-parse", "HEAD")
    except AgentError as error:
        raise AgentError("the repository has no commits yet; make an initial commit first") from error
    branch = git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    if not options.allow_dirty:
        dirty = git(repo_path, "status", "--porcelain")
        if dirty:
            raise AgentError(
                "the working tree is not clean. Commit or stash first, or pass --allow-dirty to let the "
                "agents commit the existing changes along with their own."
            )
    options.claude_bin = resolve_executable(options.claude_bin, "Claude Code")
    options.codex_bin = resolve_executable(options.codex_bin, "Codex")
    return repo_path, head, branch


def under(repo: Path, path: Path) -> Path:
    return path if path.is_absolute() else repo / path


def rounds_without_progress(findings: list[int]) -> int:
    """How many of the most recent rounds failed to get below the fewest findings before them.

    Two runs of this loop went 10, 9, 4, 4 and 8, 7, 6. The early rounds close
    substance; the late ones move the remainder around at roughly an hour each,
    and `--max-rounds` cannot tell the difference because it only counts. This is
    the number that can: a run of rounds where the review is no smaller than the
    smallest it has already been.

    Compared against the fewest seen rather than against the previous round, so
    4 -> 5 -> 4 counts as two rounds of no progress instead of resetting on the
    way back down to where it already was. A count of -1 is a verdict read from
    the REVIEW_STATUS sentinel with no number in it; an unknown quantity is
    neither progress nor the absence of it, so it is left out entirely.
    """
    fewest: int | None = None
    flat = 0
    for count in (count for count in findings if count >= 0):
        if fewest is None or count < fewest:
            fewest, flat = count, 0
        else:
            flat += 1
    return flat


def main(argv: list[str] | None = None) -> int:
    # Agent output is UTF-8; the default Windows console codepage is not. Without
    # this, the first smart quote an agent prints takes the run down.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    options = parse_args(argv)
    try:
        repo, baseline, branch = preflight(options)
    except AgentError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_FAILED

    if options.task:
        task = options.task
    elif options.task_file:
        task = options.task_file.read_text(encoding="utf-8").strip()
    else:
        task = NO_TASK
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    review_root = under(repo, options.review_dir)
    log_root = under(repo, options.log_dir)
    review_dir = paperwork_dir(repo, review_root, run_id)
    log_dir = paperwork_dir(repo, log_root, run_id)

    schema_path = log_dir / "verdict-schema.json"
    schema_path.write_text(json.dumps(VERDICT_SCHEMA, indent=2), encoding="utf-8")

    # Outside the repository by default: a checkout nested inside the tree it
    # isolates the reviewer from is not isolation, and git would have to be told
    # to ignore it.
    checkout_dir = options.review_checkout_dir or (
        Path(tempfile.gettempdir()) / "agentic-loop-review" / f"{repo.name}-{run_id}"
    )
    # Outside the repository for the same reason, and because everything under it
    # is a temporary directory an agent's tools created: none of it belongs in a
    # tree that is about to be committed, and `git add -A` in salvage_commit
    # would otherwise be the thing that swept it in.
    scratch_root = Path(tempfile.gettempdir()) / "agentic-loop-scratch" / f"{repo.name}-{run_id}"
    has_network = reviewer_network(options.codex_sandbox, options.codex_arg)

    print(f"repository : {repo}")
    print(f"branch     : {branch}")
    print(f"baseline   : {baseline[:12]}")
    print(f"run id     : {run_id}")
    default = "cli default"
    print(f"implementer: claude model={options.claude_model or default} effort={options.claude_effort or default}")
    print(f"reviewer   : codex  model={options.codex_model or default} effort={options.codex_effort or default}")
    print(f"reviews    : {review_dir}")
    print(f"logs       : {log_dir}")
    print(f"scratch    : {scratch_root} (one directory per agent per round)")
    print(f"max rounds : {options.max_rounds}")
    if options.stop_after_flat_rounds:
        print(f"stop early : after {options.stop_after_flat_rounds} rounds that do not reduce the finding count")
    print(f"starts with: {options.start_with}")
    if has_network:
        print(f"network    : reviewer has network (codex --sandbox {options.codex_sandbox})")
    else:
        print(
            f"network    : reviewer has no network (codex --sandbox {options.codex_sandbox}); it cannot open a URL "
            "or run `gh issue view`, so the task has to carry what it means"
        )
        if POINTS_OUTWARD.search(task):
            # The implementer would read it and the reviewer would not, which
            # makes the description complete for the agent writing the code and
            # empty for the one checking it against the intent.
            print(
                "warning: the task points at something only a networked agent can read, and the reviewer is not "
                "one. Paste the content into --task/--task-file instead of linking to it.",
                file=sys.stderr,
            )
    initial_range = ""
    if options.start_with == "review":
        try:
            initial_range = resolve_initial_range(repo, options, baseline)
        except AgentError as error:
            print(f"error: {error}", file=sys.stderr)
            return EXIT_FAILED
        print(f"review of  : {initial_range}")
    if options.review_checkout == "none":
        print("review tree: the implementer's working tree (--review-checkout none)")
    else:
        print(f"review tree: {checkout_dir} ({options.review_checkout})")

    rounds: list[RoundRecord] = []
    previous_review: Path | None = None
    exit_code = EXIT_ROUNDS_EXHAUSTED
    last_head = baseline
    checkout: Path | None = None

    try:
        if not options.dry_run:
            checkout = create_review_checkout(options.review_checkout, repo, checkout_dir)
        setup = ReviewSetup(options, repo, checkout, task, review_dir, log_dir, schema_path, scratch_root, has_network)

        if options.start_with == "review":
            # Round 0 reviews what is already committed. The implementer has not
            # run yet, so there is nothing of its own to report -- only a verdict.
            record = RoundRecord(number=0)
            rounds.append(record)
            print(f"\n{'=' * 72}\nround 0 (review of existing commits)\n{'=' * 72}")
            print(f"  range: {initial_range}")
            reviewed = perform_review(setup, record, 0, initial_range, None, baseline)
            if reviewed is None:  # --dry-run: the commands were printed, nothing ran
                print("\n[dry-run] stopping after the initial review; no verdict was produced.")
                exit_code = EXIT_CLEAN
                raise _Done
            verdict, review_path = reviewed
            if verdict.is_clean:
                print("\nthe existing commits reviewed clean; nothing for the implementer to do.")
                exit_code = EXIT_CLEAN
                raise _Done
            if options.max_rounds == 0:
                print("\n--max-rounds is 0: reviewed once, not handing the findings to the implementer.")
                raise _Done
            previous_review = review_path

        for number in range(1, options.max_rounds + 1):
            record = RoundRecord(number=number)
            rounds.append(record)
            print(f"\n{'=' * 72}\nround {number}/{options.max_rounds}\n{'=' * 72}")

            scratch = round_scratch(scratch_root, number, "claude")
            if previous_review is None:
                prompt = implement_prompt(task, review_dir, branch, scratch)
            else:
                prompt = fix_prompt(task, previous_review, number, branch, scratch)
            try:
                run_agent(
                    claude_command(options),
                    prompt,
                    repo,
                    f"[claude r{number}]",
                    log_dir / f"round-{number:02d}-claude.log",
                    options.timeout,
                    options.dry_run,
                    env=agent_env(scratch),
                    heartbeat=options.heartbeat,
                )
            except AgentError as error:
                # This ends the run, and the round's work is uncommitted: a round
                # commits once, at the end, and the implementer never got there.
                # Commit it before the run leaves it behind.
                salvaged = salvage_commit(repo, number, error, (review_root, log_root, checkout_dir))
                if salvaged is not None:
                    record.commits = [salvaged]
                    record.salvaged = salvaged
                    last_head = git(repo, "rev-parse", "HEAD")
                raise

            head = last_head if options.dry_run else git(repo, "rev-parse", "HEAD")
            new_commits = [] if head == last_head else git(repo, "log", "--oneline", f"{last_head}..{head}").splitlines()
            record.commits = new_commits
            if not new_commits and not options.dry_run:
                print(f"\nround {number}: Claude Code produced no commit.")
                if not options.continue_on_no_commit:
                    print("stopping as stalled; pass --continue-on-no-commit to keep going.", file=sys.stderr)
                    exit_code = EXIT_STALLED
                    break
            else:
                for line in new_commits:
                    print(f"  commit {line}")

            diff_range = f"{last_head}..{head}" if head != last_head else f"{baseline}..{head}"
            last_head = head

            reviewed = perform_review(setup, record, number, diff_range, previous_review, head)
            if reviewed is None:  # --dry-run
                print("\n[dry-run] stopping after one round; no verdict was produced.")
                exit_code = EXIT_CLEAN
                break

            verdict, review_path = reviewed
            if verdict.is_clean:
                print(f"\nreview came back clean after {number} round(s).")
                exit_code = EXIT_CLEAN
                break

            previous_review = review_path
            counts = [entry.findings for entry in rounds if entry.findings is not None and entry.findings >= 0]
            flat = rounds_without_progress(counts)
            if options.stop_after_flat_rounds and flat >= options.stop_after_flat_rounds:
                print(
                    f"\n{flat} rounds in a row have not got the review below {min(counts)} findings; stopping "
                    "rather than spending another round moving the remainder around. Raise "
                    "--stop-after-flat-rounds, or set it to 0, to keep going.",
                    file=sys.stderr,
                )
                exit_code = EXIT_NO_PROGRESS
                break
        else:
            print(f"\nreached --max-rounds ({options.max_rounds}) with findings still open.", file=sys.stderr)
    except _Done:
        pass
    except AgentError as error:
        print(f"\nerror: {error}", file=sys.stderr)
        exit_code = EXIT_FAILED
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        exit_code = EXIT_FAILED
    finally:
        if checkout is not None:
            if options.keep_review_checkout:
                print(f"\nreviewer checkout kept at {checkout}")
            else:
                remove_review_checkout(options.review_checkout, repo, checkout)
        # Per-round directories only exist so that no round inherits another
        # round's; a run that left them behind would be the next run's problem.
        # Errors are ignored on purpose: a scratch directory a tool still holds
        # open is not a reason to change what this run reports.
        if scratch_root.is_dir():
            shutil.rmtree(scratch_root, ignore_errors=True)

    summary = {
        "run_id": run_id,
        "repository": str(repo),
        "branch": branch,
        "baseline": baseline,
        "head": last_head,
        "task": task,
        "claude_model": options.claude_model,
        "claude_effort": options.claude_effort,
        "codex_model": options.codex_model,
        "codex_effort": options.codex_effort,
        "review_checkout": options.review_checkout,
        "start_with": options.start_with,
        "initial_range": initial_range or None,
        "max_rounds": options.max_rounds,
        "stop_after_flat_rounds": options.stop_after_flat_rounds,
        "reviewer_network": has_network,
        "exit_code": exit_code,
        "rounds": [vars(record) for record in rounds],
    }
    summary_path = log_dir / "run.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nsummary: {summary_path}")
    if last_head != baseline:
        print(f"commits: git log --oneline {baseline[:12]}..{last_head[:12]}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
