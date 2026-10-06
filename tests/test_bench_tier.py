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
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from support import scaled_time_bound

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


@pytest.mark.parametrize("name", ["configured_bench", "bench", "usb_uart_bench"])
def test_the_session_fixtures_report_no_setup_failure_as_a_skip(name: str) -> None:
    """The sibling entry point exits 2 on the same two conditions; one of them was wrong."""
    tree = ast.parse(BENCH_CONFTEST.read_text(encoding="utf-8"))
    fixture = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)

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
    """What the selection hook reaches through `config`: the deselection report, and the stash it keeps its reasons in."""

    def __init__(self) -> None:
        self.deselected: list[AnItem] = []
        self.hook = SimpleNamespace(pytest_deselected=lambda items: self.deselected.extend(items))
        self.stash: dict[object, object] = {}


def the_items() -> list[AnItem]:
    return [
        AnItem("tests/bench/test_bench_serial.py::test_echo", "bench"),
        AnItem("tests/bench/test_bench_without_device_group.py::test_probe", "bench", "without_device_group"),
        AnItem("tests/test_config.py::test_load"),
    ]


def selected_after(monkeypatch: pytest.MonkeyPatch, **environment: str) -> tuple[list[str], list[str]]:
    from tests.bench.conftest import BENCH_ENV, DEVICE_GROUPS_ENV, USB_UART_ENV, pytest_collection_modifyitems

    monkeypatch.delenv(BENCH_ENV, raising=False)
    monkeypatch.delenv(DEVICE_GROUPS_ENV, raising=False)
    monkeypatch.delenv(USB_UART_ENV, raising=False)
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


# -- the tests that drive the board through an STM32CubeCLT tree ------------


def test_what_runs_through_an_stm32cubeclt_tree_runs_only_where_one_is_named(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Deselected, not skipped, with no tree named or a name that is no directory; kept beside the rest where one is.

    The bench image built without `--cubeclt-archive` carries no tree, and a
    skip is what fails the tier, so a run there must leave these tests out
    rather than report them skipped. The reason is the line the run prints."""
    from tests.bench import conftest

    monkeypatch.setenv(conftest.BENCH_ENV, "1")
    monkeypatch.delenv(conftest.DEVICE_GROUPS_ENV, raising=False)
    session = "tests/bench/test_bench_stlink_sessions.py::test_a_session"
    stage = "tests/bench/test_bench_without_device_group.py::test_probe"
    rest = ["tests/bench/test_bench_serial.py::test_echo", "tests/test_config.py::test_load"]
    tree = tmp_path / "stm32cubeclt_1.22.0"
    outcomes = []
    for named in (None, str(tree), "made"):
        if named is None:
            monkeypatch.delenv(conftest.CUBECLT_ENV, raising=False)
        elif named == "made":
            tree.mkdir()
            monkeypatch.setenv(conftest.CUBECLT_ENV, str(tree))
        else:
            monkeypatch.setenv(conftest.CUBECLT_ENV, named)
        items, config = [*the_items(), AnItem(session, "bench", conftest.CUBECLT)], AConfig()
        conftest.pytest_collection_modifyitems(config, items)
        outcomes.append(([item.nodeid for item in items], [item.nodeid for item in config.deselected], conftest.pytest_report_collectionfinish(config)))

    assert outcomes[0][:2] == (rest, [stage, session])
    assert outcomes[0][2] == f"tests marked cubeclt: deselected, {conftest.CUBECLT_ENV} names no STM32CubeCLT tree for this run"
    assert outcomes[1][:2] == (rest, [stage, session])
    assert outcomes[1][2] == f"tests marked cubeclt: deselected, {conftest.CUBECLT_ENV} names {tree}, which is no directory here"
    assert outcomes[2] == ([*rest, session], [stage], None)


def test_the_stage_that_withholds_the_groups_leaves_the_stm32cubeclt_tests_out_without_a_reason_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """That stage runs one module alone, so the tree's absence is not what left these out, and the run does not say it was."""
    from tests.bench import conftest

    monkeypatch.setenv(conftest.BENCH_ENV, "1")
    monkeypatch.setenv(conftest.DEVICE_GROUPS_ENV, conftest.DEVICE_GROUPS_WITHHELD)
    monkeypatch.delenv(conftest.CUBECLT_ENV, raising=False)
    session = "tests/bench/test_bench_stlink_sessions.py::test_a_session"
    items, config = [*the_items(), AnItem(session, "bench", conftest.CUBECLT)], AConfig()

    conftest.pytest_collection_modifyitems(config, items)

    assert [item.nodeid for item in items] == ["tests/bench/test_bench_without_device_group.py::test_probe"]
    assert session in [item.nodeid for item in config.deselected]
    assert conftest.pytest_report_collectionfinish(config) is None


def test_the_stm32cubeclt_mark_is_declared_where_strict_markers_look(pytestconfig: pytest.Config) -> None:
    from tests.bench.conftest import CUBECLT

    declared = pytestconfig.getini("markers")

    assert [line for line in declared if line.startswith(f"{CUBECLT}:")], declared


def test_the_stlink_sessions_module_carries_the_stm32cubeclt_mark() -> None:
    """The module that needs the tree is the one the mark leaves out, by its own `pytestmark`."""
    from tests.bench import test_bench_stlink_sessions
    from tests.bench.conftest import CUBECLT

    assert CUBECLT in [mark.name for mark in test_bench_stlink_sessions.pytestmark]


# -- the test that can make the probe re-enumerate its serial port (#620) ----


def test_on_a_bench_a_test_that_can_reenumerate_the_probe_runs_after_every_other(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing after it shares the node its container was started with, so a
    re-enumeration fails that test alone and never the modules behind it. The
    rest keep the order they were collected in."""
    from tests.bench import conftest

    monkeypatch.setenv(conftest.BENCH_ENV, "1")
    monkeypatch.delenv(conftest.DEVICE_GROUPS_ENV, raising=False)
    killed = "tests/bench/test_bench_faults.py::test_a_flash_killed_mid_write"
    items = [the_items()[0], AnItem(killed, "bench", conftest.REENUMERATES_THE_PROBE), *the_items()[1:]]
    config = AConfig()

    conftest.pytest_collection_modifyitems(config, items)

    assert [item.nodeid for item in items] == ["tests/bench/test_bench_serial.py::test_echo", "tests/test_config.py::test_load", killed]


def test_off_a_bench_a_test_that_can_reenumerate_the_probe_keeps_its_place(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.bench import conftest

    monkeypatch.delenv(conftest.BENCH_ENV, raising=False)
    killed = "tests/bench/test_bench_faults.py::test_a_flash_killed_mid_write"
    items = [the_items()[0], AnItem(killed, "bench", conftest.REENUMERATES_THE_PROBE), *the_items()[1:]]
    before = [item.nodeid for item in items]

    conftest.pytest_collection_modifyitems(AConfig(), items)

    assert [item.nodeid for item in items] == before


def test_the_reenumeration_mark_is_declared_where_strict_markers_look(pytestconfig: pytest.Config) -> None:
    from tests.bench.conftest import REENUMERATES_THE_PROBE

    declared = pytestconfig.getini("markers")

    assert [line for line in declared if line.startswith(f"{REENUMERATES_THE_PROBE}:")], declared


def test_the_killed_flash_carries_the_reenumeration_mark() -> None:
    from tests.bench import test_bench_faults
    from tests.bench.conftest import REENUMERATES_THE_PROBE

    killed = test_bench_faults.test_a_flash_killed_mid_write_is_recovered_by_the_product_and_a_second_flash_brings_the_demo_back
    assert REENUMERATES_THE_PROBE in [mark.name for mark in getattr(killed, "pytestmark", [])]


# -- the account those tests install into, and the images it cannot install on --

STAGE = "tests/bench/test_bench_without_device_group.py::test_probe"
REST = ["tests/bench/test_bench_serial.py::test_echo", "tests/test_config.py::test_load"]
INSTALLING = "tests/bench/test_bench_first_run.py::test_upgrade"


def test_a_clean_account_is_started_without_this_tiers_interpreter_on_its_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A login shell keeps the PATH it is started with unless the distribution's
    profile sets one of its own. On the bench, Debian's did and Ubuntu's and
    Fedora's did not, so there the account's first new shell found the tier's
    own launcher, and the quick start's first line passed on an installation
    that was never the account's."""
    from tests.bench import test_bench_first_run as first_run

    skeleton = tmp_path / "skel"
    skeleton.mkdir()
    (tmp_path / "account").mkdir()
    own = str(Path(sys.executable).parent)
    system = str(tmp_path / "system")
    monkeypatch.setattr(first_run, "SKELETON", skeleton)
    monkeypatch.setenv("PATH", os.pathsep.join([own, system, own]))

    _, environment = first_run.an_account_of_its_own(tmp_path / "account", tmp_path / "index")

    assert environment["PATH"].split(os.pathsep) == [system]


def in_a_bench_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, distribution: str | None, stdout: str, returncode: int = 0
) -> list[str]:
    """This run in the bench image with its index, naming `distribution` or none,
    where a clean account's login shell prints `stdout`; what that shell is asked."""
    from tests.bench import conftest

    monkeypatch.setenv(conftest.BENCH_ENV, "1")
    monkeypatch.delenv(conftest.DEVICE_GROUPS_ENV, raising=False)
    image = tmp_path / "bench-test-image"
    image.write_text("The bench tier runs here.\n", encoding="utf-8")
    named = tmp_path / "bench-distribution"
    if distribution is not None:
        named.write_text(f"{distribution}\n", encoding="utf-8")
    index = tmp_path / "wheelhouse"
    index.mkdir()
    monkeypatch.setattr(conftest, "BENCH_IMAGE", image)
    monkeypatch.setattr(conftest, "DISTRIBUTION_NAME", named)
    monkeypatch.setattr(conftest, "WHEELHOUSE", index)
    asked: list[str] = []

    def login(script: str) -> subprocess.CompletedProcess[str]:
        asked.append(script)
        return subprocess.CompletedProcess(["bash", "-lc", script], returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(conftest, "a_clean_login_runs_python", login)
    return asked


def selected_beside_an_index_test() -> tuple[list[str], list[str], object]:
    """What the selection keeps and deselects of the usual items and one index test, and the line it reports after collection."""
    from tests.bench import conftest

    items, config = [*the_items(), AnItem(INSTALLING, "bench", conftest.NEEDS_THE_WHEELHOUSE)], AConfig()
    conftest.pytest_collection_modifyitems(config, items)
    said = conftest.pytest_report_collectionfinish(config)
    return [item.nodeid for item in items], [item.nodeid for item in config.deselected], said


def a_login_answer(tmp_path: Path, *, pip: bool, managed: bool) -> tuple[str, str, str]:
    """What the question prints, for an interpreter and a marker of this test's own."""
    python, marker = str(tmp_path / "bin" / "python"), str(tmp_path / "lib" / "EXTERNALLY-MANAGED")
    return json.dumps({"python": python, "pip": pip, "managed": marker if managed else ""}) + "\n", python, marker


@pytest.mark.parametrize(
    ("pip", "managed", "lacking"),
    [
        (False, False, "has no pip"),
        (True, True, "is marked externally managed by {marker}"),
        (False, True, "has no pip and is marked externally managed by {marker}"),
    ],
)
def test_on_a_distribution_where_a_clean_account_cannot_install_for_itself_the_index_tests_are_deselected_and_one_line_says_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pip: bool, managed: bool, lacking: str
) -> None:
    """The quick start's install line is `python -m pip install --user`. Where a
    new login shell's `python` has no pip, or is marked as the distribution's
    to manage, no newcomer gets past that line, and the stage is left out:
    deselected, since a skip fails the tier, with the image and the reason in
    one line."""
    from tests.bench import conftest

    stdout, python, marker = a_login_answer(tmp_path, pip=pip, managed=managed)
    asked = in_a_bench_image(monkeypatch, tmp_path, distribution="a-distribution", stdout=stdout)

    kept, deselected, said = selected_beside_an_index_test()

    assert asked == [conftest.ASK_ABOUT_A_USER_INSTALL]
    assert (kept, deselected) == (REST, [STAGE, INSTALLING])
    assert isinstance(said, str) and "\n" not in said, said
    assert said.startswith(f"tests marked {conftest.NEEDS_THE_WHEELHOUSE}: deselected on a-distribution, "), said
    assert "`python -m pip install --user`" in said, said
    assert said.endswith(f"its login shell's `python`, {python}, {lacking.format(marker=marker)}"), said


def test_on_a_distribution_where_a_clean_account_can_install_for_itself_the_index_tests_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stdout, _, _ = a_login_answer(tmp_path, pip=True, managed=False)
    in_a_bench_image(monkeypatch, tmp_path, distribution="a-distribution", stdout=stdout)

    kept, deselected, said = selected_beside_an_index_test()

    assert (kept, deselected, said) == ([*REST, INSTALLING], [STAGE], None)


@pytest.mark.parametrize(("stdout", "returncode"), [("", 127), ("not an answer\n", 0), ("[]\n", 0)])
def test_a_login_whose_answer_cannot_be_read_leaves_the_index_tests_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: str, returncode: int
) -> None:
    """Only a reason that was measured leaves the stage out. Without one it runs,
    and fails on whatever stopped the login, which is then in its report."""
    in_a_bench_image(monkeypatch, tmp_path, distribution="a-distribution", stdout=stdout, returncode=returncode)

    kept, deselected, said = selected_beside_an_index_test()

    assert (kept, deselected, said) == ([*REST, INSTALLING], [STAGE], None)


def test_the_default_image_runs_the_index_tests_whatever_it_lacks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """It names no distribution, and nothing there leaves the stage out: not a
    login `python` without pip, and not a missing index. The image the gate runs
    in cannot lose the stage without the stage failing."""
    from tests.bench import conftest

    stdout, _, _ = a_login_answer(tmp_path, pip=False, managed=True)
    asked = in_a_bench_image(monkeypatch, tmp_path, distribution=None, stdout=stdout)
    conftest.WHEELHOUSE.rmdir()

    kept, deselected, said = selected_beside_an_index_test()

    assert asked == []
    assert (kept, deselected, said) == ([*REST, INSTALLING], [STAGE], None)


def test_outside_the_bench_image_the_index_tests_are_deselected_and_one_line_says_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.bench import conftest

    monkeypatch.setenv(conftest.BENCH_ENV, "1")
    monkeypatch.delenv(conftest.DEVICE_GROUPS_ENV, raising=False)
    monkeypatch.setattr(conftest, "BENCH_IMAGE", tmp_path / "bench-test-image")
    monkeypatch.setattr(conftest, "WHEELHOUSE", tmp_path / "wheelhouse")

    kept, deselected, said = selected_beside_an_index_test()

    assert (kept, deselected) == (REST, [STAGE, INSTALLING])
    assert isinstance(said, str) and "\n" not in said, said
    assert said.startswith(f"tests marked {conftest.NEEDS_THE_WHEELHOUSE}: deselected, "), said
    assert conftest.WHEELHOUSE.as_posix() in said, said


def test_the_question_a_clean_login_is_asked_is_answered_by_the_interpreter_that_runs_it() -> None:
    """What it prints is what the selection reads: the interpreter, whether it
    has pip, and the marker that has its distribution manage it, if one does."""
    from tests.bench.conftest import ASK_ABOUT_A_USER_INSTALL

    answered = subprocess.run([sys.executable, "-c", ASK_ABOUT_A_USER_INSTALL], capture_output=True, text=True, timeout=scaled_time_bound(120), check=False)

    assert answered.returncode == 0, answered.stderr
    answer = json.loads(answered.stdout)
    assert set(answer) == {"python", "pip", "managed"}, answer
    assert answer["python"] == sys.executable
    assert answer["pip"] is (importlib.util.find_spec("pip") is not None)
    assert answer["managed"] == "" or Path(answer["managed"]).name == "EXTERNALLY-MANAGED", answer


# -- the tests that drive the board over the USB-UART adapter ---------------

ADAPTER = "tests/bench/test_bench_usb_uart.py::test_the_inventory_lists_the_adapter_by_its_usb_identity"


def selected_beside_an_adapter_test(monkeypatch: pytest.MonkeyPatch, *extra: AnItem, **environment: str) -> tuple[list[str], list[str], object]:
    """What a bench run keeps and deselects of the usual items, one adapter test and `extra`, and the lines it reports after collection."""
    from tests.bench import conftest

    for name in (conftest.DEVICE_GROUPS_ENV, conftest.USB_UART_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(conftest.BENCH_ENV, "1")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    items, config = [*the_items(), AnItem(ADAPTER, "bench", conftest.USB_UART), *extra], AConfig()
    conftest.pytest_collection_modifyitems(config, items)
    said = conftest.pytest_report_collectionfinish(config)
    return [item.nodeid for item in items], [item.nodeid for item in config.deselected], said


def test_a_bench_run_handed_no_adapter_deselects_its_tests_and_one_line_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deselected, not skipped, since a skip fails the tier, and said in one line
    like the tests the package index leaves out: a bench without the adapter is
    still a bench, and these tests have nothing to drive on it."""
    from tests.bench import conftest

    kept, deselected, said = selected_beside_an_adapter_test(monkeypatch)

    assert (kept, deselected) == (REST, [STAGE, ADAPTER])
    assert isinstance(said, str) and "\n" not in said, said
    assert said.startswith(f"tests marked {conftest.USB_UART}: deselected, "), said
    assert conftest.USB_UART_ENV in said, said


def test_a_bench_run_handed_the_adapter_runs_its_tests_beside_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.bench import conftest

    kept, deselected, said = selected_beside_an_adapter_test(monkeypatch, **{conftest.USB_UART_ENV: "/dev/ttyUSB0"})

    assert (kept, deselected, said) == ([*REST, ADAPTER], [STAGE], None)


def test_the_stage_without_the_device_group_speaks_of_the_adapter_only_for_its_own_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """That stage runs alone, so an adapter test outside it is deselected there
    whatever the run was handed, and is no reason for the line; one inside it
    is."""
    from tests.bench import conftest

    withheld = {conftest.DEVICE_GROUPS_ENV: conftest.DEVICE_GROUPS_WITHHELD}
    in_the_stage = "tests/bench/test_bench_without_device_group.py::test_adapter"
    staged = AnItem(in_the_stage, "bench", conftest.WITHOUT_DEVICE_GROUP, conftest.USB_UART)

    outside = selected_beside_an_adapter_test(monkeypatch, **withheld)
    inside = selected_beside_an_adapter_test(monkeypatch, staged, **withheld)
    handed = selected_beside_an_adapter_test(monkeypatch, staged, **withheld, **{conftest.USB_UART_ENV: "/dev/ttyUSB0"})

    assert outside == ([STAGE], [*REST, ADAPTER], None)
    assert inside[:2] == ([STAGE], [*REST, ADAPTER, in_the_stage])
    assert isinstance(inside[2], str) and inside[2].startswith(f"tests marked {conftest.USB_UART}: deselected, "), inside[2]
    assert handed == ([STAGE, in_the_stage], [*REST, ADAPTER], None)


def test_a_run_that_lacks_both_the_index_and_the_adapter_says_each_in_a_line_of_its_own(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tests.bench import conftest

    monkeypatch.setattr(conftest, "BENCH_IMAGE", tmp_path / "bench-test-image")
    monkeypatch.setattr(conftest, "WHEELHOUSE", tmp_path / "wheelhouse")

    kept, deselected, said = selected_beside_an_adapter_test(monkeypatch, AnItem(INSTALLING, "bench", conftest.NEEDS_THE_WHEELHOUSE))

    assert (kept, deselected) == (REST, [STAGE, ADAPTER, INSTALLING])
    assert isinstance(said, list) and len(said) == 2, said
    assert said[0].startswith(f"tests marked {conftest.NEEDS_THE_WHEELHOUSE}: deselected, "), said
    assert said[1].startswith(f"tests marked {conftest.USB_UART}: deselected, "), said


def test_off_a_bench_an_adapter_test_is_left_to_its_own_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test of the tier skips there by its own mark, and nothing is said."""
    from tests.bench import conftest

    monkeypatch.delenv(conftest.BENCH_ENV, raising=False)
    monkeypatch.delenv(conftest.USB_UART_ENV, raising=False)
    items, config = [*the_items(), AnItem(ADAPTER, "bench", conftest.USB_UART)], AConfig()

    conftest.pytest_collection_modifyitems(config, items)

    assert [item.nodeid for item in items] == [*[item.nodeid for item in the_items()], ADAPTER]
    assert (config.deselected, conftest.pytest_report_collectionfinish(config)) == ([], None)


def test_a_module_over_both_lines_marks_the_adapters_tests_and_only_those() -> None:
    """The mark is what deselects them on a bench without the adapter, so a
    parameter that lost it would run there and fail its setup instead."""
    from tests.bench.conftest import OVER_BOTH_LINES, PROBE_PORT, USB_UART

    marks = {line.values[0]: [mark.name for mark in line.marks] for line in OVER_BOTH_LINES}

    assert marks == {PROBE_PORT: [], USB_UART: [USB_UART]}


def test_the_adapter_mark_is_declared_where_strict_markers_look(pytestconfig: pytest.Config) -> None:
    from tests.bench.conftest import USB_UART

    declared = pytestconfig.getini("markers")

    assert [line for line in declared if line.startswith(f"{USB_UART}:")], declared
