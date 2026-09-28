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
from types import SimpleNamespace

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


# -- the stage that withholds the probe's group ----------------------------


class AnItem:
    """A collected test as the tier's selection reads one: its id and its marks."""

    def __init__(self, nodeid: str, *marks: str) -> None:
        self.nodeid = nodeid
        self.marks = set(marks)

    def get_closest_marker(self, name: str) -> object | None:
        return name if name in self.marks else None


class AConfig:
    """What the selection hook reaches through `config`: the deselection report."""

    def __init__(self) -> None:
        self.deselected: list[AnItem] = []
        self.hook = SimpleNamespace(pytest_deselected=lambda items: self.deselected.extend(items))


def the_items() -> list[AnItem]:
    return [
        AnItem("tests/bench/test_bench_serial.py::test_echo", "bench"),
        AnItem("tests/bench/test_bench_without_device_group.py::test_probe", "bench", "without_device_group"),
        AnItem("tests/test_config.py::test_load"),
    ]


def selected_after(monkeypatch: pytest.MonkeyPatch, **environment: str) -> tuple[list[str], list[str]]:
    from tests.bench.conftest import BENCH_ENV, DEVICE_GROUPS_ENV, pytest_collection_modifyitems

    monkeypatch.delenv(BENCH_ENV, raising=False)
    monkeypatch.delenv(DEVICE_GROUPS_ENV, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    items, config = the_items(), AConfig()
    pytest_collection_modifyitems(config, items)
    return [item.nodeid for item in items], [item.nodeid for item in config.deselected]


def test_off_a_bench_the_selection_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test of the tier skips there by its own mark, the stage's included."""
    kept, deselected = selected_after(monkeypatch)

    assert kept == [item.nodeid for item in the_items()]
    assert deselected == []


def test_a_bench_that_keeps_the_groups_never_runs_the_stage_that_withholds_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deselected, not skipped: a skip is what fails the tier, and the stage
    cannot pass where the groups it withholds are still there."""
    from tests.bench.conftest import BENCH_ENV

    kept, deselected = selected_after(monkeypatch, **{BENCH_ENV: "1"})

    assert kept == ["tests/bench/test_bench_serial.py::test_echo", "tests/test_config.py::test_load"]
    assert deselected == ["tests/bench/test_bench_without_device_group.py::test_probe"]


def test_a_bench_that_withholds_the_groups_runs_that_stage_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every other test opens the probe, and the container it runs in may not."""
    from tests.bench.conftest import BENCH_ENV, DEVICE_GROUPS_ENV, DEVICE_GROUPS_WITHHELD

    kept, deselected = selected_after(monkeypatch, **{BENCH_ENV: "1", DEVICE_GROUPS_ENV: DEVICE_GROUPS_WITHHELD})

    assert kept == ["tests/bench/test_bench_without_device_group.py::test_probe"]
    assert deselected == ["tests/bench/test_bench_serial.py::test_echo", "tests/test_config.py::test_load"]


def test_the_stage_mark_is_declared_where_strict_markers_look(pytestconfig: pytest.Config) -> None:
    declared = pytestconfig.getini("markers")

    assert [line for line in declared if line.startswith("without_device_group:")], declared


# -- the tests that install the product from the bench image's index -------


def test_what_installs_from_the_bench_images_index_runs_only_where_that_index_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Deselected, not skipped, on a bench without the index; kept beside the rest on one that has it."""
    from tests.bench import conftest

    monkeypatch.setenv(conftest.BENCH_ENV, "1")
    monkeypatch.delenv(conftest.DEVICE_GROUPS_ENV, raising=False)
    monkeypatch.setattr(conftest, "WHEELHOUSE", tmp_path / "wheelhouse")
    installing = "tests/bench/test_bench_first_run.py::test_upgrade"
    selections = []
    for _ in ("without the index", "with it"):
        items, config = [*the_items(), AnItem(installing, "bench", conftest.NEEDS_THE_WHEELHOUSE)], AConfig()
        conftest.pytest_collection_modifyitems(config, items)
        selections.append(([item.nodeid for item in items], [item.nodeid for item in config.deselected]))
        conftest.WHEELHOUSE.mkdir(exist_ok=True)

    stage = "tests/bench/test_bench_without_device_group.py::test_probe"
    rest = ["tests/bench/test_bench_serial.py::test_echo", "tests/test_config.py::test_load"]
    assert selections == [(rest, [stage, installing]), ([*rest, installing], [stage])]


def test_the_index_mark_is_declared_where_strict_markers_look(pytestconfig: pytest.Config) -> None:
    from tests.bench.conftest import NEEDS_THE_WHEELHOUSE

    declared = pytestconfig.getini("markers")

    assert [line for line in declared if line.startswith(f"{NEEDS_THE_WHEELHOUSE}:")], declared
