"""install.sh over a uv tool that carries more than its root requirement, on the two arms that never read the receipt.

`install_with_uv` merges what uv recorded into the line it reinstalls from,
but only on the `refresh` arm. A `--version` pin goes to the `pin` arm and a
run that finds no `agentic-hil` on PATH goes to the default arm, and both
reinstall from this run's package spec alone. Real uv then does what it is
told: it records the requirement it was handed and uninstalls everything the
old receipt had beside it. Measured with uv 0.12.10: `install.sh --version
<the release the tool already ran>` over a tool whose receipt recorded
`agentic-hil[can]` and `requests>=2` printed `- requests==2.34.2` and four
more removals and left a receipt naming the pinned distribution alone, with
nothing in the transcript about it. README.md promises the opposite of a
rerun: it "repairs what is already installed rather than forcing the public
release over it".

The default arm is the shape #430 described: a newcomer runs the one-liner
again from a shell whose PATH does not yet carry uv's bin, step 1 finds no
`agentic-hil` and calls the run fresh, and the tool uv already owns is rebuilt
from the bare spec.

Both cases run here against the real uv and the real index: the receipt
writer and the uninstaller are what is under test, and a stub uv that kept
the `--with` would only prove the stub kind. `iniconfig` stands in for the
issue's `requests`: one small pure-Python package with nothing beneath it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from .conftest import CONTAINER_ONLY, INSTALL_TIMEOUT_S, REPOSITORY_ROOT, UvTool

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

SHELL_SCRIPT = REPOSITORY_ROOT / "install.sh"


def receipt_document(uv_tool: UvTool) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - only a 3.10 collection reaches this
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib.loads(uv_tool.receipt.read_text(encoding="utf-8"))


def recorded_names(document: dict) -> dict[str, dict]:
    return {str(entry["name"]): entry for entry in document["tool"]["requirements"]}


def installed_version(uv_tool: UvTool) -> str:
    answered = uv_tool.run("--version")
    assert answered.returncode == 0, answered.stderr
    return answered.stdout.strip()


def run_install(uv_tool: UvTool, project: Path, *arguments: str, tool_bin_on_path: bool) -> subprocess.CompletedProcess[str]:
    """This checkout's install.sh, with the real uv on PATH and the tool's bin on it or not."""
    path = os.environ.get("PATH", "")
    if tool_bin_on_path:
        path = f"{uv_tool.bin_directory}{os.pathsep}{path}"
    return subprocess.run(
        ["sh", str(SHELL_SCRIPT), "--no-agent-install", *arguments],
        cwd=str(project),
        capture_output=True,
        text=True,
        env=uv_tool.environment(PATH=path),
        timeout=INSTALL_TIMEOUT_S,
        check=False,
    )


def a_tool_with_an_extra_and_a_with(uv_tool: UvTool) -> dict:
    """`uv tool install 'agentic-hil[can]' --with 'iniconfig>=2'` from the index, and its receipt.

    The receipt is read back and asserted before anything is rerun over it,
    so a later assertion about what survived is about a record that was there.
    """
    uv_tool.install("agentic-hil[can]", "--with", "iniconfig>=2")
    before = recorded_names(receipt_document(uv_tool))
    assert before["agentic-hil"].get("extras") == ["can"], before
    assert "specifier" not in before["agentic-hil"], before
    assert before["iniconfig"].get("specifier") == ">=2", before
    return before


def test_a_pinned_rerun_keeps_the_recorded_extras_and_with_requirements(uv_tool: UvTool, tmp_path: Path) -> None:
    """`install.sh --version <the release already installed>` over a tool with a `--with`.

    The pin is the release the tool already runs, so the only thing this run
    can change is the record: the version stays, the extra stays, and the
    `--with` requirement stays, because a repair that removed a package the
    operator installed beside the tool would be a different installation
    wearing the same command. The receipt is uv's own, read after the run.
    """
    a_tool_with_an_extra_and_a_with(uv_tool)
    release = installed_version(uv_tool)
    project = tmp_path / "project"
    project.mkdir()

    pinned = run_install(uv_tool, project, "--version", release, tool_bin_on_path=True)

    transcript = f"{pinned.stdout}{pinned.stderr}"
    assert pinned.returncode == 0, transcript
    assert f"--version {release} was asked for" in transcript, transcript
    after = recorded_names(receipt_document(uv_tool))
    assert "iniconfig" in after, (after, transcript)
    assert after["iniconfig"].get("specifier") == ">=2", after
    assert after["agentic-hil"].get("extras") == ["can"], after
    assert after["agentic-hil"].get("specifier") == f"=={release}", after
    assert installed_version(uv_tool) == release, transcript
    # uv's own removal lines are the sign the record was rebuilt from the bare
    # spec: none may appear for a package the receipt recorded.
    assert not re.search(r"^\s*- iniconfig==", transcript, re.MULTILINE), transcript


def test_a_rerun_that_finds_no_launcher_on_path_still_keeps_what_uv_recorded(uv_tool: UvTool, tmp_path: Path) -> None:
    """The #430 shape: uv on PATH, uv's bin not, so step 1 calls the run fresh.

    Step 1 decides `fresh` by whether `agentic-hil` resolves and not by asking
    uv whether it owns one. The default arm's `uv tool install --upgrade
    agentic-hil[can]` then rebuilds the tool uv already holds from that spec
    alone. Whatever step 1 calls the run, the tool uv owns has a receipt, and
    the reinstall has to carry it.
    """
    a_tool_with_an_extra_and_a_with(uv_tool)
    release = installed_version(uv_tool)
    project = tmp_path / "project"
    project.mkdir()

    rerun = run_install(uv_tool, project, tool_bin_on_path=False)

    transcript = f"{rerun.stdout}{rerun.stderr}"
    assert rerun.returncode == 0, transcript
    after = recorded_names(receipt_document(uv_tool))
    assert "iniconfig" in after, (after, transcript)
    assert after["iniconfig"].get("specifier") == ">=2", after
    assert after["agentic-hil"].get("extras") == ["can"], after
    assert installed_version(uv_tool) == release, transcript
    assert not re.search(r"^\s*- iniconfig==", transcript, re.MULTILINE), transcript
