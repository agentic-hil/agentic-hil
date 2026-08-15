"""What the built eval image really does, asked of the image itself.

Every other test here reads the Dockerfile. The defect these exist for could not
be seen that way: the PATH guard was declared correctly in `ENV PATH` and was
still absent from the shell codex runs its commands in, because `/etc/profile`
rewrites PATH from scratch and the declaration says nothing about that. Only the
running image answers it.

These skip rather than build. Building takes an `npm ci` and a pinned pip
install, which is not something a unit test run should trigger; the message says
how to build it when they skip.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

IMAGE = os.environ.get("AGENTIC_HIL_EVAL_IMAGE", "agentic-hil-install-eval:local")
BUILD_HINT = f"build it with: python -m evals.install build --image {IMAGE}"
DOCKERFILE = Path(__file__).resolve().parents[1] / "evals" / "install" / "container" / "Dockerfile"


def _evidence(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout}{result.stderr}\nimage: {IMAGE}"


def _built_at(docker: str) -> datetime | None:
    """When this image was built, or None if this machine cannot answer.

    None covers the absent image and the unreachable daemon alike: a healthy
    daemon answers an inspect in milliseconds, and one that hangs the whole
    timeout is a machine these tests must skip on, not fail on."""
    try:
        result = subprocess.run(
            [docker, "image", "inspect", "--format", "{{.Created}}", IMAGE],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        return datetime.fromisoformat(result.stdout.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _in_image(*arguments: str) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is not installed on this machine")
    built = _built_at(docker)
    if built is None:
        pytest.skip(f"the eval image {IMAGE} is not reachable here (no daemon, or not built); {BUILD_HINT}")
    # An image built before the Dockerfile it should come from answers about a
    # tree nobody is asking about. That is not a failing image, it is a stale
    # one, and this asks about the image rather than about the checkout it lags.
    if built < datetime.fromtimestamp(DOCKERFILE.stat().st_mtime, tz=timezone.utc):
        pytest.skip(f"the eval image {IMAGE} was built before this Dockerfile; {BUILD_HINT}")
    return subprocess.run(
        [docker, "run", "--rm", "--network", "none", "--entrypoint", "/bin/bash", IMAGE, *arguments],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_a_login_shell_cannot_reach_a_shadowed_hardware_command() -> None:
    """The 57 codex runs of the last full matrix were blind to exactly this."""
    result = _in_image("-lc", "openocd --version")

    assert result.returncode == 126, _evidence(result)
    assert "forbidden in the Agentic HIL install eval" in result.stderr


def test_a_login_shell_resolves_the_hardware_command_to_the_guard() -> None:
    result = _in_image("-lc", "command -v openocd")

    assert result.returncode == 0, _evidence(result)
    assert result.stdout.strip() in {"/opt/eval-guard/bin/openocd", "/usr/local/bin/openocd"}


def test_a_login_shell_keeps_the_guard_directory_in_front_of_its_path() -> None:
    """Belt and braces: the shims are also reachable without the profile drop-in."""
    result = _in_image("-lc", "echo $PATH")

    assert result.returncode == 0, _evidence(result)
    assert result.stdout.split(":")[0].strip() == "/opt/eval-guard/bin"


def test_the_image_resolves_the_openocd_scripts_a_generated_config_names() -> None:
    """Both containers of a run come from this image, so both resolve these."""
    result = _in_image(
        "-lc",
        "test -f /usr/share/openocd/scripts/interface/stlink.cfg "
        "&& test -f /usr/share/openocd/scripts/target/stm32f4x.cfg",
    )

    assert result.returncode == 0, _evidence(result)
