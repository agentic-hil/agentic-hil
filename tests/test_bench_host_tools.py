"""One rule for a missing host tool in the bench tier (#535).

A bench with the probe, the board, the cross compiler and OpenOCD, but no
`arm-none-eabi-gdb`, answered with nineteen failures and thirteen errors, every
one of them the same missing executable met again by the next test that needed
it. The tier had a treatment for a missing build tool, a skip, and none for the
debugger, so each debug test found its absence in the product's own refusal.

The rule these tests hold the tier to has two halves. A missing host tool, an
executable the host provides such as the cross compiler, the build tools or the
cross debugger, skips the tests that need it, with one sentence naming the
executable and where it was looked for. The probe and the board stay strict:
under `AGENTIC_HIL_BENCH=1` a hardware setup step that cannot be completed is a
failure, because the variable is the operator's statement that they are there.
A run that skipped a half says so at the end, so that it cannot be read as a
full tier that passed.

The tier needs hardware, so the rule is tested from here. The debugger check is
called directly, and the rest runs through `pytester`: a small module in the
tier's shape, with the tier's own conftest loaded as a plugin, the probe and the
board replaced by fakes, and a PATH that holds no host tool at all.
`AGENTIC_HIL_BENCH` is set only for the length of those inner runs, and nothing
here touches a probe, a board or a real debugger.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import FAKE_GDB, write_config
from support import scaled_time_bound

from agentic_hil.backends.gdbdebug import no_gdb_on_this_bench
from agentic_hil.config import GDB_AUTODETECT_CANDIDATES, ConfigError, load_authoritative_config
from tests.bench import conftest as bench_tier

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# The parts of the tier the closing summary accounts for, by the name it gives
# each: what needs only the probe and the board, what needs the demo firmware
# built, and what needs a debugger on top of that.
HALVES = ("hardware tests", "build half", "debug half")

# The debugger the product looks for first when the configuration names none,
# and the build tool the tier's `firmware` fixture looks for first.
CROSS_DEBUGGER = "arm-none-eabi-gdb"
BUILD_TOOL = "cmake"

# The first line of the skip the tier's `firmware` fixture raises for a build
# that ran and failed, and a stand-in for the build's own output after it. A
# build that fails on a host with every tool is not a missing host tool.
DID_NOT_BUILD = "the demo firmware did not build here: cmake --build --preset Debug"
BUILD_OUTPUT = "the build's own output follows the first line here"

# One inner run is a pytest start, one child resolving the debugger and seven
# empty tests. The ceiling is there for a run that hangs, not to time one.
INNER_RUN_TIMEOUT_S = 120.0

# The tests of the small module below, by the half each one belongs to.
HARDWARE_TESTS = ["test_hardware_probe_answers", "test_hardware_board_resets"]
BUILD_TESTS = ["test_build_firmware_flashes", "test_build_firmware_answers_after_reset"]
DEBUG_TESTS = ["test_debug_session_stops_at_main", "test_debug_breakpoint_is_hit", "test_debug_symbol_is_read"]
ALL_TESTS = HARDWARE_TESTS + BUILD_TESTS + DEBUG_TESTS

# A module in the tier's shape. Its `bench` stands in for the tier's own, with a
# probe that answers or with none, its `firmware` for a build and a flash that
# need no toolchain and no board, or for a build that failed, where the run asks
# for that, and its `probe` for the first thing a real test does with the
# hardware: the log it writes is how the test outside sees which tests got that
# far. `gdb` is not defined here. It comes from the tier's conftest, loaded as a
# plugin, as in the real tier.
TINY_TIER = '''
"""The bench tier's shape, with the hardware replaced by fakes."""

import json
from pathlib import Path

import pytest

from tests.bench.conftest import BENCH_ONLY, Bench, refuse

pytestmark = [pytest.mark.bench, BENCH_ONLY]

SETTINGS = json.loads(Path(__file__).with_name("tiny_bench.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def bench():
    if not SETTINGS["probe_attached"]:
        refuse("this bench is not bound to hardware, so no plan can run against it: no probe answered")
    return Bench(
        project=Path(SETTINGS["project"]),
        config=Path(SETTINGS["config"]),
        config_root=Path(SETTINGS["config_root"]),
        state_root=Path(SETTINGS["state_root"]),
    )


if SETTINGS["fake_firmware"]:

    @pytest.fixture(scope="session")
    def firmware(bench):
        if SETTINGS["build_failure"] is not None:
            pytest.skip(SETTINGS["build_failure"])
        return bench.project / "build" / "Debug" / "nucleo-f446re_demo.elf"


@pytest.fixture
def probe(request):
    with Path(SETTINGS["probe_log"]).open("a", encoding="utf-8") as log:
        print(request.node.name, file=log)


def test_hardware_probe_answers(bench, probe):
    pass


def test_hardware_board_resets(bench, probe):
    pass


def test_build_firmware_flashes(firmware, probe):
    pass


def test_build_firmware_answers_after_reset(firmware, probe):
    pass


def test_debug_session_stops_at_main(gdb, firmware, probe):
    pass


def test_debug_breakpoint_is_hit(gdb, firmware, probe):
    pass


def test_debug_symbol_is_read(gdb, firmware, probe):
    pass
'''

TINY_TIER_INI = """
[pytest]
markers =
    bench: needs a debug probe and the board behind it
"""


@pytest.fixture
def no_host_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PATH with nothing on it, and the tier's children kept inside this test's sandbox."""
    empty = tmp_path / "path-without-host-tools"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    home = tmp_path / "operator-home"
    home.mkdir()
    monkeypatch.setattr(bench_tier, "OPERATOR_HOME", str(home))
    return empty


def a_configured_bench(tmp_path: Path, *, gdb_executable: Path | None = None) -> bench_tier.Bench:
    """A bench whose configuration is on disk, laid out the way `init` lays out the tier's own."""
    project = tmp_path / "project"
    config_root = tmp_path / "config"
    config = write_config(
        project,
        gdb_executable=gdb_executable,
        config_path=config_root / "agentic-hil" / "projects" / "demo" / "config.yaml",
    )
    return bench_tier.Bench(project=project, config=config, config_root=config_root, state_root=tmp_path / "state")


def the_debugger_check() -> Callable[[bench_tier.Bench], str | None]:
    """The tier's check for the debug half, looked up so that its absence fails a test and not the module."""
    check = getattr(bench_tier, "missing_gdb", None)
    assert check is not None, "the bench tier has no check that resolves the debugger the way the product does"
    return check


def what_the_check_says(bench: bench_tier.Bench) -> str | None:
    """Why the debug half cannot run on `bench`, or None where it can.

    A skip raised out of the check is turned into a failure here. A check that
    skipped the test asking it would leave this file green having asserted
    nothing, which is the report this issue is about.
    """
    check = the_debugger_check()
    try:
        return check(bench)
    except pytest.skip.Exception as skipped:
        pytest.fail(f"the check skipped the test that asked it instead of answering: {skipped}", pytrace=False)


def the_products_refusal_of(bench: bench_tier.Bench, monkeypatch: pytest.MonkeyPatch) -> ConfigError:
    """What the product says of this bench's configuration, loaded the way its server loads it."""
    monkeypatch.setenv("AGENTIC_HIL_CONFIG", str(bench.config))
    with pytest.raises(ConfigError) as refused:
        load_authoritative_config(bench.project)
    return refused.value


def spellings(path: Path) -> set[str]:
    """The ways a sentence can name `path` and still be naming it."""
    resolved = path.resolve()
    return {str(path), path.as_posix(), str(resolved), resolved.as_posix()}


def a_tiny_tier(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    *,
    declared: bool = True,
    probe_attached: bool = True,
    fake_firmware: bool = True,
    build_failure: str | None = None,
    gdb_executable: Path | None = None,
) -> Path:
    """The small module, its bench's configuration and the environment it runs in; returns the probe's log."""
    project = pytester.path / "project"
    config_root = pytester.path / "config"
    config = write_config(
        project,
        gdb_executable=gdb_executable,
        config_path=config_root / "agentic-hil" / "projects" / "demo" / "config.yaml",
    )
    probe_log = pytester.path / "probe.log"
    settings = {
        "project": str(project),
        "config": str(config),
        "config_root": str(config_root),
        "state_root": str(pytester.path / "state"),
        "probe_log": str(probe_log),
        "probe_attached": probe_attached,
        "fake_firmware": fake_firmware,
        "build_failure": build_failure,
    }
    (pytester.path / "tiny_bench.json").write_text(json.dumps(settings), encoding="utf-8")
    pytester.makepyfile(test_tiny_bench=TINY_TIER)
    pytester.makeini(TINY_TIER_INI)
    monkeypatch.setenv("PYTHONPATH", str(REPOSITORY_ROOT), prepend=os.pathsep)
    if declared:
        monkeypatch.setenv(bench_tier.BENCH_ENV, "1")
    else:
        monkeypatch.delenv(bench_tier.BENCH_ENV, raising=False)
    return probe_log


def run_the_tiny_tier(pytester: pytest.Pytester, *arguments: str) -> pytest.RunResult:
    """The small module under the tier's own conftest, in a fresh interpreter, with every skip listed."""
    return pytester.runpytest_subprocess(
        "-p",
        "tests.bench.conftest",
        "-p",
        "no:cacheprovider",
        "-rs",
        *arguments,
        timeout=scaled_time_bound(INNER_RUN_TIMEOUT_S),
    )


def the_tier_summary(result: pytest.RunResult) -> dict[str, str] | None:
    """The closing `bench tier` section, as the line it gives each half, or None where the run wrote none."""
    lines = [line.strip() for line in result.outlines]
    starts = [index for index, line in enumerate(lines) if re.fullmatch(r"=+ bench tier =+", line)]
    if not starts:
        return None
    halves: dict[str, str] = {}
    for line in lines[starts[0] + 1 :]:
        if line.startswith("="):
            break
        for half in HALVES:
            if line.startswith(half):
                halves[half] = line
    return halves


def ran(line: str | None) -> bool:
    return line is not None and re.search(r"\bran\b", line) is not None and "skipped" not in line


def skipped_naming(line: str | None, tool: str) -> bool:
    return line is not None and "skipped" in line and "missing host tool" in line and tool in line


def who_touched_the_probe(probe_log: Path) -> list[str]:
    if not probe_log.exists():
        return []
    return probe_log.read_text(encoding="utf-8").split()


def debugger_checks(result: pytest.RunResult) -> list[str]:
    """The lines `--setup-show` prints each time the `gdb` check is set up."""
    return [line for line in result.outlines if re.match(r"\s*SETUP\s+S gdb\b", line)]


# -- the rule, where the tier states it -------------------------------------


def test_the_tier_states_one_rule_for_a_missing_host_tool_and_keeps_the_hardware_strict() -> None:
    """Both halves, in the module docstring, where an operator reading the tier looks first."""
    docstring = " ".join((bench_tier.__doc__ or "").lower().split())

    for words in ("host tool", "compiler", "debugger", "skip"):
        assert words in docstring, f"the tier's docstring does not say what a missing host tool earns: no {words!r}"
    for words in ("probe", "board", "failure", "operator"):
        assert words in docstring, f"the tier's docstring does not keep the probe and the board strict: no {words!r}"


# -- the debugger check, asked directly -------------------------------------


def test_a_debugger_found_nowhere_is_one_sentence_naming_it_and_where_it_was_looked_for(
    tmp_path: Path, no_host_tools: Path
) -> None:
    """Nothing configured and nothing on PATH: every name, where it was looked for, and what the product says.

    Every name the product looks for, out of the product's own list. A sentence
    naming only the first would tell an operator with another of them in reach
    that the bench needs a debugger it already has.
    """
    why = what_the_check_says(a_configured_bench(tmp_path))

    assert why is not None, "a bench with no debugger anywhere was taken for one that has one"
    for candidate in GDB_AUTODETECT_CANDIDATES:
        named = re.search(rf"(?<![\w-]){re.escape(candidate)}(?![\w-])", why)
        assert named is not None, f"the sentence does not name {candidate}, which the product looks for: {why}"
    assert "PATH" in why, why
    assert "debug.gdb_executable" in why and "not set" in why, why
    assert no_gdb_on_this_bench("openocd")["summary"] in why, why
    assert "\n" not in why, why


def test_a_configured_debugger_outside_path_is_found_the_way_the_product_finds_it(
    tmp_path: Path, no_host_tools: Path
) -> None:
    """The product runs a configured path whatever PATH holds, so a check by name on PATH would skip a working bench."""
    assert what_the_check_says(a_configured_bench(tmp_path, gdb_executable=FAKE_GDB)) is None


def test_a_debugger_the_product_finds_under_another_name_is_not_missing(tmp_path: Path, no_host_tools: Path) -> None:
    """A bench with `gdb-multiarch` and no `arm-none-eabi-gdb` debugs, so a check for the one name would skip it."""
    other = "gdb-multiarch"
    assert other in GDB_AUTODETECT_CANDIDATES, f"the product does not look for {other}, so this test asks nothing"
    stand_in = no_host_tools / (other + (".exe" if os.name == "nt" else ""))
    stand_in.write_bytes(b"")
    stand_in.chmod(0o755)
    assert shutil.which(other) is not None and shutil.which(CROSS_DEBUGGER) is None, "the PATH is not this test's"

    assert what_the_check_says(a_configured_bench(tmp_path)) is None


def test_a_configured_debugger_that_does_not_exist_is_a_missing_host_tool_named_by_its_path(
    tmp_path: Path, no_host_tools: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Still a host package, and the sentence points at the configuration, which is where the cause is."""
    gone = tmp_path / "removed-toolchain" / "bin" / CROSS_DEBUGGER
    bench = a_configured_bench(tmp_path, gdb_executable=gone)

    why = what_the_check_says(bench)

    assert why is not None, "a configured debugger that does not exist was taken for one that does"
    assert any(spelling in why for spelling in spellings(gone)), why
    assert "debug.gdb_executable" in why, why
    assert the_products_refusal_of(bench, monkeypatch).summary in why, why
    assert "\n" not in why, why


def test_a_configured_debugger_name_that_is_not_on_path_is_a_missing_host_tool_named_by_it(
    tmp_path: Path, no_host_tools: Path
) -> None:
    """A bare name, which the product looks up on PATH: the sentence names it and PATH, not a file."""
    why = what_the_check_says(a_configured_bench(tmp_path, gdb_executable=Path(CROSS_DEBUGGER)))

    assert why is not None, "a configured debugger name found nowhere was taken for one that is there"
    assert CROSS_DEBUGGER in why and "PATH" in why and "debug.gdb_executable" in why, why
    assert "\n" not in why, why


def test_a_configured_debugger_that_is_there_and_refused_is_a_failure_with_the_products_line(
    tmp_path: Path, no_host_tools: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a missing host tool: the file exists and the product refuses it for where it is."""
    check = the_debugger_check()
    inside = tmp_path / "project" / "toolchain" / CROSS_DEBUGGER
    bench = a_configured_bench(tmp_path, gdb_executable=inside)
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(b"")
    refused = the_products_refusal_of(bench, monkeypatch)

    with pytest.raises(BaseException) as raised:  # noqa: B017 - a skip has to be caught here too, and then refused
        check(bench)

    assert raised.typename == "Failed", (
        f"a debugger that is there and refused ended in {raised.typename}: {raised.value}"
    )
    assert refused.summary in str(raised.value), str(raised.value)


def test_a_configured_debugger_is_never_replaced_by_one_found_on_path(tmp_path: Path, no_host_tools: Path) -> None:
    """The product never swaps a configured GDB for another, so the check does not either."""
    stand_in = no_host_tools / (CROSS_DEBUGGER + (".exe" if os.name == "nt" else ""))
    stand_in.write_bytes(b"")
    stand_in.chmod(0o755)
    assert shutil.which(CROSS_DEBUGGER) is not None, (
        "the stand-in on PATH is not found by name, so this test asks nothing"
    )
    gone = tmp_path / "removed-toolchain" / "bin" / CROSS_DEBUGGER

    why = what_the_check_says(a_configured_bench(tmp_path, gdb_executable=gone))

    assert why is not None, f"the check accepted the {CROSS_DEBUGGER} on PATH for the one the configuration names"
    assert any(spelling in why for spelling in spellings(gone)), why


def test_a_check_that_cannot_run_is_a_failure_that_keeps_the_products_own_line(
    tmp_path: Path, no_host_tools: Path
) -> None:
    """Not a missing host tool: a bench whose configuration cannot be read was not set up."""
    check = the_debugger_check()
    project = tmp_path / "project"
    project.mkdir()
    bench = bench_tier.Bench(
        project=project,
        config=tmp_path / "config" / "nothing-was-written-here.yaml",
        config_root=tmp_path / "config",
        state_root=tmp_path / "state",
    )

    with pytest.raises(BaseException) as raised:  # noqa: B017 - a skip has to be caught here too, and then refused
        check(bench)

    assert raised.typename == "Failed", f"a check that could not run ended in {raised.typename}: {raised.value}"
    assert "configuration file could not be found" in str(raised.value), str(raised.value)


# -- the tier, run --------------------------------------------------------


def test_a_bench_without_the_debugger_skips_the_debug_half_once_and_runs_the_rest(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, no_host_tools: Path
) -> None:
    """The issue's bench: the probe and the board answer, and no `arm-none-eabi-gdb` is on PATH."""
    probe_log = a_tiny_tier(pytester, monkeypatch)

    result = run_the_tiny_tier(pytester, "--setup-show")

    naming_the_debugger = [line for line in result.outlines if line.startswith("SKIPPED") and CROSS_DEBUGGER in line]
    assert len(naming_the_debugger) == 1 and naming_the_debugger[0].startswith(f"SKIPPED [{len(DEBUG_TESTS)}] "), (
        f"the debug half was not skipped as one line with one reason naming {CROSS_DEBUGGER}: {naming_the_debugger}"
    )
    result.assert_outcomes(passed=len(HARDWARE_TESTS) + len(BUILD_TESTS), skipped=len(DEBUG_TESTS))
    assert len(debugger_checks(result)) == 1, (
        f"the debugger was not checked once for the session: {debugger_checks(result)}"
    )
    assert sorted(who_touched_the_probe(probe_log)) == sorted(HARDWARE_TESTS + BUILD_TESTS), (
        "a debug test reached the probe"
    )
    summary = the_tier_summary(result)
    assert summary is not None, "a bench-tier run ended with no summary saying which halves ran"
    assert skipped_naming(summary.get("debug half"), CROSS_DEBUGGER), summary
    assert ran(summary.get("hardware tests")), summary
    assert ran(summary.get("build half")), summary


def test_a_bench_whose_configured_debugger_resolves_runs_every_half(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, no_host_tools: Path
) -> None:
    """A GDB the configuration names, outside PATH: the product runs it, so the tier runs the debug half."""
    probe_log = a_tiny_tier(pytester, monkeypatch, gdb_executable=FAKE_GDB)

    result = run_the_tiny_tier(pytester)

    summary = the_tier_summary(result)
    assert summary is not None, "a bench-tier run ended with no summary saying which halves ran"
    for half in HALVES:
        assert ran(summary.get(half)), summary
    result.assert_outcomes(passed=len(ALL_TESTS))
    assert sorted(who_touched_the_probe(probe_log)) == sorted(ALL_TESTS)


def test_a_missing_probe_still_fails_every_test_and_skips_none(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, no_host_tools: Path
) -> None:
    """The strict half, on a PATH without a debugger too: what the run reports is the probe."""
    probe_log = a_tiny_tier(pytester, monkeypatch, probe_attached=False)

    result = run_the_tiny_tier(pytester)

    summary = the_tier_summary(result)
    assert summary is not None, "a bench-tier run ended with no summary saying which halves ran"
    for half in HALVES:
        line = summary.get(half)
        assert line is not None and not ran(line) and "skipped" not in line, summary
    result.assert_outcomes(errors=len(ALL_TESTS))
    result.stdout.fnmatch_lines(["*not bound to hardware*"])
    assert who_touched_the_probe(probe_log) == []


def test_a_run_that_is_not_a_bench_skips_everything_and_writes_no_summary(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, no_host_tools: Path
) -> None:
    """Without the variable the tier is inert: a developer's pytest gets no check and no section."""
    probe_log = a_tiny_tier(pytester, monkeypatch, declared=False)

    result = run_the_tiny_tier(pytester, "--setup-show")

    result.assert_outcomes(skipped=len(ALL_TESTS))
    assert the_tier_summary(result) is None, "a run that is not a bench wrote the bench tier's summary"
    assert debugger_checks(result) == [], "a run that is not a bench looked for a debugger"
    assert who_touched_the_probe(probe_log) == []


def test_a_bench_without_the_build_tools_names_what_each_half_lacked(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, no_host_tools: Path
) -> None:
    """The tier's own `firmware` check, on a PATH with no `cmake`, beside the debugger check."""
    probe_log = a_tiny_tier(pytester, monkeypatch, fake_firmware=False)

    result = run_the_tiny_tier(pytester)

    naming_the_build_tool = [line for line in result.outlines if line.startswith("SKIPPED") and BUILD_TOOL in line]
    assert len(naming_the_build_tool) == 1 and naming_the_build_tool[0].startswith(f"SKIPPED [{len(BUILD_TESTS)}] "), (
        f"the build half was not skipped as one line with one reason naming {BUILD_TOOL}: {naming_the_build_tool}"
    )
    summary = the_tier_summary(result)
    assert summary is not None, "a bench-tier run ended with no summary saying which halves ran"
    assert skipped_naming(summary.get("build half"), BUILD_TOOL), summary
    assert skipped_naming(summary.get("debug half"), CROSS_DEBUGGER), summary
    assert ran(summary.get("hardware tests")), summary
    result.assert_outcomes(passed=len(HARDWARE_TESTS), skipped=len(BUILD_TESTS) + len(DEBUG_TESTS))
    assert sorted(who_touched_the_probe(probe_log)) == sorted(HARDWARE_TESTS)


def test_a_half_the_run_did_not_select_is_reported_as_not_selected(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, no_host_tools: Path
) -> None:
    """A `-k` that picks only the tests the probe and the board need: the other two halves neither ran nor skipped."""
    probe_log = a_tiny_tier(pytester, monkeypatch)

    result = run_the_tiny_tier(pytester, "-k", "test_hardware", "--setup-show")

    result.assert_outcomes(passed=len(HARDWARE_TESTS), deselected=len(BUILD_TESTS) + len(DEBUG_TESTS))
    summary = the_tier_summary(result)
    assert summary is not None, "a bench-tier run ended with no summary saying which halves ran"
    assert ran(summary.get("hardware tests")), summary
    for half in ("build half", "debug half"):
        line = summary.get(half)
        assert line is not None and "not selected" in line, summary
        assert re.search(r"\bran\b", line) is None and "skipped" not in line, summary
    assert debugger_checks(result) == [], "a run that selected no debug test looked for a debugger"
    assert sorted(who_touched_the_probe(probe_log)) == sorted(HARDWARE_TESTS)


def test_a_build_that_fails_is_skipped_on_its_own_lines_and_not_as_a_missing_host_tool(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, no_host_tools: Path
) -> None:
    """Every tool there and the build failing: pytest's own report for that skip, and its first line in the section."""
    probe_log = a_tiny_tier(
        pytester, monkeypatch, build_failure=f"{DID_NOT_BUILD}\n{BUILD_OUTPUT}", gdb_executable=FAKE_GDB
    )

    result = run_the_tiny_tier(pytester)

    result.assert_outcomes(passed=len(HARDWARE_TESTS), skipped=len(BUILD_TESTS) + len(DEBUG_TESTS))
    not_built = [line for line in result.outlines if line.startswith("SKIPPED") and DID_NOT_BUILD in line]
    assert len(not_built) == len(BUILD_TESTS) + len(DEBUG_TESTS), (
        f"a build that failed was not reported test by test, as pytest reports a skip: {not_built}"
    )
    assert all(line.startswith("SKIPPED [1] ") for line in not_built), not_built
    summary = the_tier_summary(result)
    assert summary is not None, "a bench-tier run ended with no summary saying which halves ran"
    assert ran(summary.get("hardware tests")), summary
    for half in ("build half", "debug half"):
        line = summary.get(half)
        assert line is not None and "skipped" in line and DID_NOT_BUILD in line, summary
        assert "missing host tool" not in line and BUILD_OUTPUT not in line, summary
    assert sorted(who_touched_the_probe(probe_log)) == sorted(HARDWARE_TESTS)
