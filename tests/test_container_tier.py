"""What the container tier's gate has to distinguish, and what the image it names is.

The container job's whole claim is that it ran the real uv, the real OpenOCD and
the real `/proc`. The gate that decides whether it did is a `skipif`, and a
`skipif` cannot tell "this run never meant to be here" from "this run said it was
in the image and the image cannot answer". The first is a developer's `pytest`
and is a skip. The second is a required check reporting success over fifteen
tests that measured nothing, and it has to stop the collection instead.

The image marker is the other half. An environment variable is something an
operator can export on a Linux bench with a probe plugged in, and one test in
this tier runs `agentic-hil init`, which reads the attached bench. A file the
image build writes cannot be exported by accident.

This module runs on any host. It never sets the tier's variable and never
imports the tools the tier needs.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPOSITORY_ROOT / "tools" / "container" / "Dockerfile"
DOCKERIGNORE = REPOSITORY_ROOT / "tools" / "container" / "Dockerfile.dockerignore"
CONTAINER_README = REPOSITORY_ROOT / "tools" / "container" / "README.md"
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"

from tests.container.conftest import (  # noqa: E402
    CONTAINER_ENV,
    IMAGE_MARKER,
    a_line_within,
    image_gate,
    missing_from_the_image,
    why_this_run_is_not_in_the_image,
)


def everything_is_there(name: str) -> str | None:
    return f"/usr/bin/{name}"


def nothing_is_there(name: str) -> str | None:
    return None


# -- M2: a skip and an error are not the same answer ----------------------


def test_a_run_that_never_declared_the_image_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's `pytest` in a checkout collects this tier and runs none of it."""
    monkeypatch.delenv(CONTAINER_ENV, raising=False)

    why = why_this_run_is_not_in_the_image()

    assert why is not None
    assert CONTAINER_ENV in why
    assert image_gate(why, "uv is not on PATH") is None


def test_a_declared_run_whose_image_cannot_answer_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Green over fifteen skips and green over fifteen passes were indistinguishable."""
    monkeypatch.setenv(CONTAINER_ENV, "1")

    assert why_this_run_is_not_in_the_image() is None
    with pytest.raises(RuntimeError) as raised:
        image_gate(None, "uv is not on PATH, and this tier reads receipts uv writes")

    assert "uv is not on PATH" in str(raised.value)
    assert CONTAINER_ENV in str(raised.value)


def test_a_declared_run_in_a_whole_image_passes_the_gate() -> None:
    assert image_gate(None, None) is None


def test_each_tool_the_tier_needs_is_named_when_it_is_missing(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    marker.write_text("image\n", encoding="utf-8")
    proc = tmp_path / "proc"
    proc.mkdir()

    assert missing_from_the_image(which=everything_is_there, proc_root=proc, marker=marker) is None
    assert "uv" in (missing_from_the_image(which=nothing_is_there, proc_root=proc, marker=marker) or "")
    assert str(tmp_path / "gone") in (missing_from_the_image(which=everything_is_there, proc_root=tmp_path / "gone", marker=marker) or "")


# -- m6: what says this is the image, rather than what says somebody meant it


def test_the_marker_the_image_writes_is_what_admits_the_tier(tmp_path: Path) -> None:
    """A firmware developer's Linux bench satisfies every other condition with a probe attached."""
    proc = tmp_path / "proc"
    proc.mkdir()

    why = missing_from_the_image(which=everything_is_there, proc_root=proc, marker=tmp_path / "no-marker")

    assert why is not None
    assert str(tmp_path / "no-marker") in why


def test_the_image_build_writes_the_marker_the_gate_looks_for() -> None:
    """The gate looks for a file, so the build has to be the thing that writes it."""
    assert IMAGE_MARKER.as_posix() in DOCKERFILE.read_text(encoding="utf-8")


# -- m8: a server that starts and never answers ---------------------------


def test_a_pipe_that_never_answers_gives_the_reader_back(tmp_path: Path) -> None:
    """The readline sits ahead of every assertion, with no timeout plugin anywhere."""
    read, write = os.pipe()
    stream = os.fdopen(read, encoding="utf-8")
    try:
        assert a_line_within(stream, 0.2) is None
    finally:
        os.close(write)
        stream.close()


def test_a_pipe_that_answers_hands_the_line_over(tmp_path: Path) -> None:
    read, write = os.pipe()
    stream = os.fdopen(read, encoding="utf-8")
    try:
        with os.fdopen(write, "w", encoding="utf-8") as writing:
            writing.write("one line\n")
        assert a_line_within(stream, 5.0) == "one line\n"
    finally:
        stream.close()


# -- m9, m10, m11: what the image is built out of -------------------------


def test_the_build_context_admits_what_the_image_needs_and_nothing_else() -> None:
    """The ignore file omitted every local path the repository's own already names."""
    ignore = [line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]

    assert ignore[0] == "**", ignore
    admitted = {line.lstrip("!").rstrip("/*").rstrip("/") for line in ignore if line.startswith("!")}
    assert {"pyproject.toml", "src", "tests", "requirements"} <= admitted, admitted


def test_the_base_image_is_pinned_by_digest() -> None:
    """Two builds of one commit produced a different interpreter and a different debugger."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    base = next(line for line in dockerfile.splitlines() if line.startswith("FROM "))

    assert "@sha256:" in base, base


def test_the_image_installs_the_dependency_set_the_repository_already_locked() -> None:
    """The matrix job installs this set with hashes; the required check resolved it live."""
    commands = [line for line in DOCKERFILE.read_text(encoding="utf-8").splitlines() if line.startswith("RUN ")]

    assert "requirements/dev.txt" in DOCKERFILE.read_text(encoding="utf-8")
    locked = next(line for line in commands if "dev.txt" in line)
    assert "--require-hashes" in locked, locked
    checkout = next(line for line in commands if line.rstrip().endswith("-e ."))
    assert "--no-deps" in checkout, checkout


def test_the_readme_claims_only_the_pinning_the_file_has() -> None:
    """The list said what was pinned, and named two things that were not."""
    readme = CONTAINER_README.read_text(encoding="utf-8")
    pinned = readme.split("## What is pinned in the image", 1)[1].split("##", 1)[0]

    assert "@sha256:" in pinned or "by digest" in pinned
    assert "requirements/dev.txt" in pinned


# -- M2 and m12: what the job proves, and what wires it to the merge gate --


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_container_job_writes_a_report_and_refuses_a_run_that_measured_nothing() -> None:
    """Nothing in the job checked that anything ran, and a fully skipped tier exits 0."""
    steps = workflow()["jobs"]["container_tests"]["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)

    assert "--junitxml" in commands
    assert "skipped" in commands
    assert "errors" in commands


def test_the_container_job_is_part_of_the_required_check() -> None:
    """Delete either line and the job runs, reports, and blocks nothing."""
    jobs = workflow()["jobs"]
    required = jobs["required-ci"]

    assert "container_tests" in required["needs"]
    assert "needs.container_tests.result" in "\n".join(step.get("run", "") for step in required["steps"])


def test_the_container_tier_is_collected_by_a_bare_pytest_and_runs_nothing() -> None:
    """The tier is published in this repository, so a checkout has to be able to collect it."""
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/container", "tests/bench", "--collect-only", "-q"],
        capture_output=True,
        text=True,
        cwd=str(REPOSITORY_ROOT),
        env={key: value for key, value in os.environ.items() if key not in {CONTAINER_ENV, "AGENTIC_HIL_BENCH"}},
        check=False,
    )

    assert collected.returncode == 0, collected.stdout + collected.stderr
    assert "tests/container/test_process_table.py" in collected.stdout.replace("\\", "/"), collected.stdout
    assert "tests/bench/test_bench_plans.py" in collected.stdout.replace("\\", "/"), collected.stdout


# -- #480 and #488: two more tools the image has to carry, and be checked for --


def only_missing(name: str):
    return lambda asked: None if asked == name else f"/usr/bin/{asked}"


def test_a_missing_pyocd_is_named_by_the_gate(tmp_path: Path) -> None:
    """pyOCD with no probe is exactly the #480 bench, and only this image can be it.

    A pyOCD spawned without `-W` waits for a probe forever, and the only place
    that can be measured with nothing attached and nothing at risk is here. A
    run that declared the image and has no pyOCD would skip the one test that
    proves the refusal, and report green over it.
    """
    marker = tmp_path / "marker"
    marker.write_text("image\n", encoding="utf-8")
    proc = tmp_path / "proc"
    proc.mkdir()

    why = missing_from_the_image(which=only_missing("pyocd"), proc_root=proc, marker=marker)

    assert why is not None
    assert "pyocd" in why


def test_a_missing_curl_is_named_by_the_gate(tmp_path: Path) -> None:
    """The fetch route of install.sh runs Astral's pinned installer, and both need curl.

    #488 found that route editing shell rc files, which no fake of the installer
    reproduces: the test that pins it has to fetch the real bytes and run them,
    here, and `install.sh` reaches for curl or wget to do that.
    """
    marker = tmp_path / "marker"
    marker.write_text("image\n", encoding="utf-8")
    proc = tmp_path / "proc"
    proc.mkdir()

    why = missing_from_the_image(which=only_missing("curl"), proc_root=proc, marker=marker)

    assert why is not None
    assert "curl" in why


def test_the_image_carries_pyocd_and_curl() -> None:
    """The gate looks for both, so the build has to be what installs them.

    pyOCD from the package index at one pinned version, named the way the same
    file names `UV_VERSION` and for the same reason: the fixture under
    tests/fixtures reproduces what pyOCD 0.45.1 printed, a different release may
    word its refusal differently, and the drift test in the tier should go red
    on a deliberate bump with a test run behind it, not on a rebuild. The reason
    the distribution's own packages stay unpinned (the mirror drops superseded
    versions) does not reach pyOCD: the package index keeps every release. curl
    from the distribution, because install.sh fetches the pinned Astral
    installer with it.
    """
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    commands = [line for line in dockerfile.splitlines() if line.startswith("RUN ")]

    apt = next(line for line in commands if "apt-get" in line)
    apt_block = dockerfile[dockerfile.index(apt) :].split("\n\n", 1)[0]
    assert "curl" in apt_block, apt_block
    pinned = re.search(r"^ARG PYOCD_VERSION=(\d+\.\d+\.\d+)$", dockerfile, re.MULTILINE)
    assert pinned is not None, "pyocd is not pinned by an ARG the way uv is"
    pip_lines = [line for line in dockerfile.splitlines() if "pip install" in line or line.strip().startswith('"')]
    assert any("pyocd==${PYOCD_VERSION}" in line for line in pip_lines), pip_lines
