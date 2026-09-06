"""What uv actually writes into its receipt, read by the code that has to read it.

``uv tool install`` records how an installation was created, and that record is
the whole of what a reinstall line has to reproduce: the requirement with its
extras, every ``--with`` package beside it, and the interpreter. Everything the
suite knew about that file until now came from a fixture somebody typed, and the
fixture was wrong in the one way that mattered: uv 0.12.9 writes ``python`` at
the ``[tool]`` level, not under ``[tool.options]``, so a reader that looked only
in the second place produced a reinstall line with no ``--python`` at all, under
a summary promising to rebuild the installation as it stands. A line that
rebuilds an installation on a different interpreter is a different installation
wearing the same command.

So these tests install with the real uv and read the real file. They assert the
layout as well as the values, because the layout is the part a fake cannot know.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import ABOVE_EVERY_RELEASE, COMMAND_TIMEOUT_S, CONTAINER_ONLY, UvTool, Wheelhouse

pytestmark = [pytest.mark.container, CONTAINER_ONLY]


def receipt_document(uv_tool: UvTool) -> dict:
    """uv's receipt, parsed, so a test can ask where a key sits and not only whether it is there."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - only a 3.10 collection reaches this
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib.loads(uv_tool.receipt.read_text(encoding="utf-8"))


def test_the_receipt_records_the_interpreter_under_tool_and_the_reader_finds_it(uv_tool: UvTool, wheelhouse: Wheelhouse) -> None:
    """The exact shape #456 was measured against, and every part of the record beside it.

    One install carries all of it: an extra, a bare ``--with``, a ``--with`` with
    a version specifier, a ``--with`` with an environment marker, an exact
    requirement and an explicit interpreter. The assertions below are one per
    thing a reinstall line owes the operator, and each of them is a line that
    silently disappeared from such a line at some point.
    """
    interpreter = os.path.realpath(sys.executable)
    uv_tool.install(
        "--python",
        interpreter,
        "--find-links",
        str(wheelhouse.every_version),
        f"agentic-hil[can]=={ABOVE_EVERY_RELEASE}",
        "--with",
        "pytest",
        "--with",
        "pytest-timeout>=2",
        "--with",
        "iniconfig; python_version>='3.10'",
    )

    document = receipt_document(uv_tool)
    recorded = uv_tool.recorded_install()

    # The layout itself, which is the fact a hand-written fixture got wrong: the
    # interpreter is a member of `[tool]`, and `[tool.options]` does not carry it.
    assert document["tool"]["python"] == interpreter, document
    assert "python" not in document["tool"].get("options", {}), document
    # And the reader finds it where uv put it. This is the assertion that trips
    # if `_recorded_python` goes back to looking only under `[tool.options]`: it
    # answers the empty string, and the reinstall line loses its `--python`.
    assert recorded["python"] == interpreter, recorded

    assert recorded["pin"] == f"=={ABOVE_EVERY_RELEASE}", recorded
    assert recorded["exact_pin"] == f"=={ABOVE_EVERY_RELEASE}", recorded
    beside = list(recorded["with_requirements"])
    assert "pytest" in beside, beside
    assert "pytest-timeout>=2" in beside, beside
    # uv normalises the marker it was given, so what is pinned here is that a
    # marker survives into the `--with` value at all: leaving it out took the
    # package off the installation the line claimed to preserve.
    marked = [entry for entry in beside if entry.startswith("iniconfig")]
    assert len(marked) == 1, beside
    assert ";" in marked[0] and "3.10" in marked[0], marked
    # Nothing in this receipt is unspellable, so nothing is reported as left behind.
    assert recorded["not_replayed"] == [], recorded


def test_the_receipt_records_no_interpreter_when_none_was_asked_for(uv_tool: UvTool, wheelhouse: Wheelhouse) -> None:
    """The other direction, because a reader that invents one is as wrong as a reader that misses one.

    A reinstall line carrying a ``--python`` the operator never chose pins an
    installation to whichever interpreter happened to be resolved on the day, and
    the empty record is what keeps that line silent about it.
    """
    uv_tool.install("--find-links", str(wheelhouse.every_version), f"agentic-hil=={ABOVE_EVERY_RELEASE}")

    document = receipt_document(uv_tool)
    recorded = uv_tool.recorded_install()

    assert "python" not in document["tool"], document
    assert "python" not in document["tool"].get("options", {}), document
    assert recorded["python"] == "", recorded
    assert recorded["with_requirements"] == [], recorded


def test_a_wheel_installed_by_path_records_a_source_the_reader_reports_no_pin_for(uv_tool: UvTool, wheelhouse: Wheelhouse) -> None:
    """uv records a path, not a version, and nothing may read that as a pin.

    An installation created from a file has no version requirement behind it, so
    a reader that turned the recorded source into a specifier would refuse an
    upgrade naming a pin the operator never wrote.
    """
    uv_tool.install(str(wheelhouse.wheels[ABOVE_EVERY_RELEASE]))

    document = receipt_document(uv_tool)
    recorded = uv_tool.recorded_install()

    requirement = document["tool"]["requirements"][0]
    assert "path" in requirement, requirement
    assert "specifier" not in requirement, requirement
    assert recorded["pin"] == "", recorded
    assert recorded["exact_pin"] == "", recorded


def test_the_installation_knows_which_manager_holds_it_and_where_its_receipt_is(uv_tool: UvTool, wheelhouse: Wheelhouse) -> None:
    """The two facts every assertion above rests on, asserted rather than assumed.

    ``_uv_receipt`` reads ``sys.prefix``/``uv-receipt.toml`` rather than asking
    uv where its tool directory is, and it reads it only for an installation
    ``owning_manager`` calls ``uv-tool``. An installation this answers anything
    else for is one whose receipt is never consulted, so the pin, the extras and
    the ``--with`` packages travel nowhere and every result above would be about
    a different code path than the one it names.
    """
    uv_tool.install("--find-links", str(wheelhouse.every_version), f"agentic-hil=={ABOVE_EVERY_RELEASE}")

    reported = subprocess.run(
        [str(uv_tool.interpreter), "-c", "import sys; from agentic_hil.upgrade import owning_manager; print(owning_manager()); print(sys.prefix)"],
        capture_output=True,
        text=True,
        env=uv_tool.environment(),
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )

    assert reported.returncode == 0, reported.stderr
    manager, prefix = reported.stdout.split()
    assert manager == "uv-tool", reported.stdout
    assert Path(prefix) == uv_tool.environment_root, reported.stdout
    assert uv_tool.receipt.is_file(), sorted(path.name for path in uv_tool.environment_root.iterdir())
