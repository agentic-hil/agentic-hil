"""What `agentic-hil upgrade` answers when a real uv is the one answering it.

Four outcomes, and the suite's fakes could only ever assert the shape of them.
What decides which one an operator gets is what uv writes into its receipt and
what uv's resolver does with it, and both of those are uv's to decide:

* an exact pin below an available release is a refusal, and the line it hands
  the operator has to rebuild the installation whole,
* a floor is not a pin and has to be moved rather than reported as a block,
* a recorded `exclude-newer` is what uv offers no command to clear, so it is
  named as what holds the installation back,
* and no result may call an installation current until the index has said so.

Every installation below is created by the real `uv tool install` from wheels of
this checkout built at version numbers chosen so the question has an answer:
above everything the index publishes where the test needs a resolution it
controls, below everything it publishes where the test needs the index to be
ahead. The wheels carry this tree's own code, so what answers is the code under
test.
"""

from __future__ import annotations

import os
import sys

import pytest

from .conftest import (
    ABOVE_EVERY_RELEASE,
    BELOW_EVERY_RELEASE,
    CONTAINER_ONLY,
    ONE_RELEASE_ABOVE_THAT,
    RECORDED_EXCLUDE_NEWER,
    UvTool,
    Wheelhouse,
    a_closed_local_port,
)

pytestmark = [pytest.mark.container, CONTAINER_ONLY]


def every_route_to_a_closed_port() -> dict[str, str]:
    """Proxy variables that make every outbound request fail at once.

    A closed port on the loopback interface refuses the connection immediately,
    so a test that wants "the index did not answer" gets that answer in
    milliseconds rather than by waiting out a timeout. `no_proxy` is cleared
    because a bypass entry left in the environment would route the request after
    all and the test would silently stop testing anything.
    """
    dead = f"http://127.0.0.1:{a_closed_local_port()}"
    return {"http_proxy": dead, "https_proxy": dead, "all_proxy": dead, "HTTP_PROXY": dead, "HTTPS_PROXY": dead, "ALL_PROXY": dead, "no_proxy": None, "NO_PROXY": None}


def test_a_wheel_installed_by_path_is_judged_against_the_index_before_it_is_called_current(uv_tool: UvTool, wheelhouse: Wheelhouse) -> None:
    """The installation uv has nothing newer for, and what the result may say about it.

    `uv tool upgrade` answers `Nothing to upgrade` for an installation created
    from a file, and that sentence is about this installation's own recorded
    resolution and nothing else. The claim that this is the newest release there
    is belongs to the index, so the result carries what the index published and
    says in words where this installation sits against it. This wheel is above
    every release the index publishes, which is the case that used to be reported
    as `and this installation is on it`, over a build the index had never seen.
    """
    uv_tool.install(str(wheelhouse.wheels[ABOVE_EVERY_RELEASE]))

    status, result = uv_tool.upgrade()

    assert status == 0, result
    assert result["ok"] is True, result
    assert result["already_current"] is True, result
    assert result["previous_version"] == ABOVE_EVERY_RELEASE, result
    assert result["version"] == ABOVE_EVERY_RELEASE, result
    # The index answered, which this tier needs the network for; without that
    # answer the summary says so instead and this assertion names why.
    assert "newest_release" in result, f"the release index did not answer, so this run proves nothing about the currency claim: {result['summary']}"
    assert result["newest_release"] != ABOVE_EVERY_RELEASE, result
    assert f"ahead of it at {ABOVE_EVERY_RELEASE}" in result["summary"], result["summary"]


def test_an_exact_pin_below_an_available_release_is_refused_with_a_line_that_keeps_the_installation_whole(uv_tool: UvTool, wheelhouse: Wheelhouse) -> None:
    """The refusal, and the one thing the refusal is for.

    uv prints its own hint here, `reinstall with uv tool install
    agentic-hil@latest`, and following it is what cost a pinned bench the pytest
    it had installed with `--with`. So the line this result names has to carry
    every part of the record: the extras, the packages the receipt lists beside
    the distribution, and the interpreter the installation was created with.
    """
    interpreter = os.path.realpath(sys.executable)
    uv_tool.install(
        "--python",
        interpreter,
        "--find-links",
        str(wheelhouse.only(ABOVE_EVERY_RELEASE)),
        f"agentic-hil[can]=={ABOVE_EVERY_RELEASE}",
        "--with",
        "pytest",
    )

    status, result = uv_tool.upgrade(UV_FIND_LINKS=str(wheelhouse.every_version))

    assert status == 1, result
    assert result["ok"] is False, result
    assert result["error_type"] == "upgrade_blocked_by_pin", result
    assert result["pinned_version"] == ABOVE_EVERY_RELEASE, result
    # Nothing moved, which is the other half of a refusal: an operator told an
    # upgrade was blocked must not find one happened anyway.
    assert result["previous_version"] == ABOVE_EVERY_RELEASE, result
    assert result["version"] == ABOVE_EVERY_RELEASE, result
    assert result.get("upgraded_on_disk") is not True, result

    assert result["installed_extras"] == ["can"], result
    assert result["with_packages"] == ["pytest"], result
    assert result["recorded_python"] == interpreter, result
    reinstall = result["reinstall_command"]
    # The three parts a bare `uv tool install agentic-hil@latest` drops.
    assert "--with pytest" in reinstall, reinstall
    assert f"--python {interpreter}" in reinstall, reinstall
    assert "agentic-hil[can]@latest" in reinstall, reinstall


def test_a_recorded_floor_is_moved_rather_than_reported_as_a_block(uv_tool: UvTool, wheelhouse: Wheelhouse) -> None:
    """A `>=` is not a pin, and reading it as one refused an upgrade nothing was holding.

    The installation is created from a wheelhouse holding one release, so the
    floor resolves there; the upgrade is then offered a wheelhouse holding the
    next one. A `>=` moves with what is available by design, which is what
    AI_AGENT_QUICKSTART.md recommends operators install with, so this is the case
    a provisioning script runs `agentic-hil upgrade` on unconditionally.
    """
    uv_tool.install("--find-links", str(wheelhouse.only(ABOVE_EVERY_RELEASE)), f"agentic-hil[can]>={ABOVE_EVERY_RELEASE}")

    status, result = uv_tool.upgrade(UV_FIND_LINKS=str(wheelhouse.every_version))

    assert status == 0, result
    assert result["ok"] is True, result
    assert result["upgraded_on_disk"] is True, result
    assert result["previous_version"] == ABOVE_EVERY_RELEASE, result
    assert result["version"] == ONE_RELEASE_ABOVE_THAT, result
    assert "error_type" not in result, result
    assert "held_back_by" not in result, result


def test_a_recorded_exclude_newer_is_named_as_what_holds_this_installation_back(uv_tool: UvTool, wheelhouse: Wheelhouse) -> None:
    """The option uv mentions nowhere and offers no command to clear.

    A receipt carrying `exclude-newer` makes every later resolution pass over
    anything published since, so `Nothing to upgrade` arrives on an installation
    that is a release behind and means nothing about the index. Reading it as
    `already_current` put that claim on a bench that was behind, and pointed the
    follow-up sentence at the recorded requirement, which was not what held it.

    The recorded requirement here is a compatible-release clause rather than an
    exact one, so what is left to explain the outcome is the option alone.
    """
    uv_tool.install(
        "--find-links",
        str(wheelhouse.only(BELOW_EVERY_RELEASE)),
        "--exclude-newer",
        RECORDED_EXCLUDE_NEWER,
        f"agentic-hil[can]~={BELOW_EVERY_RELEASE}",
    )

    status, result = uv_tool.upgrade(UV_FIND_LINKS=str(wheelhouse.every_version))

    assert status == 1, result
    assert result["ok"] is False, result
    assert result["error_type"] == "upgrade_blocked_by_recorded_option", result
    # The claim is withdrawn rather than qualified: an installation the index has
    # something newer for is not current, whatever the manager said.
    assert "already_current" not in result, result
    assert result["version"] == BELOW_EVERY_RELEASE, result
    held = result["held_back_by"]
    assert len(held) == 1, held
    assert "exclude-newer" in held[0], held
    assert RECORDED_EXCLUDE_NEWER in held[0], held
    assert result["newest_release"] != BELOW_EVERY_RELEASE, result
    assert result["reinstall_command"].startswith("uv tool install"), result


def test_an_index_that_did_not_answer_takes_the_currency_claim_away(uv_tool: UvTool, wheelhouse: Wheelhouse) -> None:
    """`Nothing to upgrade` is a package manager's sentence, not the index's.

    uv resolves from its own cache here and reaches no network at all, so it
    still answers, while every route the release-index read could take is a
    closed port. That is the split this behaviour exists for: the manager is
    happy, and whether this is the newest release there is has not been
    established, so the result says that rather than claiming it.

    The requirement is recorded unpinned, which is the shape the installers write
    and the shape this rule is about: an installation whose own receipt names one
    release is a separate outcome with a separate summary.
    """
    uv_tool.install("--find-links", str(wheelhouse.only(ONE_RELEASE_ABOVE_THAT)), "agentic-hil")

    status, result = uv_tool.upgrade(UV_OFFLINE="1", **every_route_to_a_closed_port())

    assert status == 0, result
    assert result["ok"] is True, result
    assert result["install"]["returncode"] == 0, result
    assert "already_current" not in result, result
    assert "newest_release" not in result, result
    assert "The newest release could not be checked" in result["summary"], result["summary"]
