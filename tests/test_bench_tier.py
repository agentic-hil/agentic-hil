"""What the bench tier's own setup has to establish before it calls a bench green.

`tests/bench` is this branch's hardware gate: the only place a probe and a board
answer for themselves. A gate is worth exactly what its setup is worth, and the
three things that setup has to get right cannot be checked on a bench, because a
bench that is wrong about them reports success. So they are checked here, off the
bench, out of the tier's own helpers:

* the child command is this checkout and not whatever an installation on the
  machine put on `sys.path`;
* the redirect that keeps the operator's configuration out of reach moves the
  variables the product reads on this platform, and the configuration `init`
  reports is under the root this run redirected to;
* a bench that was declared and could not be set up fails. A skip there is a
  green tier that executed nothing on hardware.

Nothing in this module touches hardware or sets the tier's own variable.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BENCH_CONFTEST = REPOSITORY_ROOT / "tests" / "bench" / "conftest.py"

from tests.bench.conftest import (  # noqa: E402
    CHECKOUT_SOURCES,
    Bench,
    child_command,
    not_the_checkout,
    outside_this_runs_root,
    refuse,
)


def a_bench(tmp_path: Path) -> Bench:
    return Bench(
        project=tmp_path / "project",
        config=tmp_path / "config" / "agentic-hil" / "projects" / "demo" / "config.yaml",
        config_root=tmp_path / "config",
        state_root=tmp_path / "state",
    )


# -- M1: which copy of the product the gate drives ------------------------


def test_the_child_command_leaves_the_user_site_directory_out() -> None:
    """A regular install in the per-user site directory outranks an editable checkout."""
    command = child_command("doctor")

    assert command[0] == sys.executable
    assert "-s" in command[: command.index("-m")]
    assert command[-2:] == ["agentic_hil", "doctor"]


def test_the_bench_environment_puts_this_checkout_first_on_the_import_path(tmp_path: Path) -> None:
    environment = a_bench(tmp_path).environment()

    assert environment["PYTHONPATH"].split(os.pathsep)[0] == str(CHECKOUT_SOURCES)
    assert CHECKOUT_SOURCES == REPOSITORY_ROOT / "src"


def test_a_product_resolved_outside_this_checkout_is_named_and_refused(tmp_path: Path) -> None:
    """The gate reporting on the wrong code, with nothing on the surface saying so."""
    shadow = tmp_path / "site-packages" / "agentic_hil" / "__init__.py"

    why = not_the_checkout(str(shadow))

    assert why is not None
    assert str(shadow) in why


def test_the_checkouts_own_package_is_accepted() -> None:
    assert not_the_checkout(str(CHECKOUT_SOURCES / "agentic_hil" / "__init__.py")) is None


# -- m5: the redirect the tier's docstring promises -----------------------


def test_the_bench_environment_moves_the_platform_configuration_and_state_roots(tmp_path: Path) -> None:
    """The XDG pair alone is inert on Windows, and the docstring says otherwise."""
    bench = a_bench(tmp_path)

    environment = bench.environment()

    assert environment["XDG_CONFIG_HOME"] == str(bench.config_root)
    assert environment["XDG_STATE_HOME"] == str(bench.state_root)
    assert environment["APPDATA"] == str(bench.config_root)
    assert environment["LOCALAPPDATA"] == str(bench.state_root)


def test_the_operators_home_still_reaches_the_commands_that_take_the_device_locks(tmp_path: Path) -> None:
    """Deliberate, and pinned unchanged: the locks are what keeps this run off a held board."""
    from tests.bench.conftest import OPERATOR_HOME

    environment = a_bench(tmp_path).environment()

    assert environment["HOME"] == OPERATOR_HOME
    assert environment["USERPROFILE"] == OPERATOR_HOME


def test_a_configuration_outside_this_runs_root_is_named_and_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "operator" / "config.yaml"

    why = outside_this_runs_root(elsewhere, tmp_path / "config")

    assert why is not None
    assert str(elsewhere) in why


def test_a_configuration_under_this_runs_root_is_accepted(tmp_path: Path) -> None:
    inside = tmp_path / "config" / "agentic-hil" / "projects" / "demo" / "config.yaml"

    assert outside_this_runs_root(inside, tmp_path / "config") is None


# -- m1: a declared bench that cannot be set up ---------------------------


def test_a_declared_bench_that_cannot_be_set_up_is_a_failure(tmp_path: Path) -> None:
    """`AGENTIC_HIL_BENCH=1` is the operator saying a probe and a board are attached."""
    with pytest.raises(BaseException) as raised:  # noqa: B017 - pytest.fail's own exception, by construction
        refuse("this bench is not bound to hardware")

    assert raised.typename == "Failed"
    assert "not bound to hardware" in str(raised.value)


def test_the_session_fixture_reports_no_setup_failure_as_a_skip() -> None:
    """The sibling entry point exits 2 on the same two conditions; one of them was wrong."""
    tree = ast.parse(BENCH_CONFTEST.read_text(encoding="utf-8"))
    fixture = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "bench")

    skips = [node for node in ast.walk(fixture) if isinstance(node, ast.Attribute) and node.attr == "skip"]

    assert skips == [], "a setup failure in a declared bench is a failure, not a skip"
