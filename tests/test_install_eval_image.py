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

import pytest

IMAGE = os.environ.get("AGENTIC_HIL_EVAL_IMAGE", "agentic-hil-install-eval:local")
BUILD_HINT = f"build it with: python -m evals.install build --image {IMAGE}"


def _evidence(result: subprocess.CompletedProcess[str]) -> str:
    """Say what the image did, and that an image built before this test is stale."""
    return f"{result.stdout}{result.stderr}\nif {IMAGE} predates this test, rebuild it: {BUILD_HINT}"


def _in_image(*arguments: str) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is not installed on this machine")
    present = subprocess.run(
        [docker, "image", "inspect", IMAGE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if present.returncode != 0:
        pytest.skip(f"the eval image {IMAGE} is not built here; {BUILD_HINT}")
    return subprocess.run(
        [docker, "run", "--rm", "--network", "none", "--entrypoint", "/bin/bash", IMAGE, *arguments],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_a_login_shell_cannot_reach_a_shadowed_hardware_command() -> None:
    """The 57 codex runs of the 0.12.0 matrix were blind to exactly this."""
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
