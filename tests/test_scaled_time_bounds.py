"""One scaling factor for the suite's wall-clock bounds (#515).

Two bounds went red on a loaded machine with nothing wrong in the code under
test, and each was green when its file ran alone: the probeless pyOCD refusal
measured 5.07 s against a bound of 5.0 s, and a live run's heartbeat read
1.324 s old against a bound of 1.0 s.

The bounds are right for what they pin. One says a refusal comes back promptly
instead of waiting out `timeout_s`; the other says a heartbeat stays fresh
through a long step. What neither carried was an allowance for the scheduler,
so a contended host cost a rerun and taught nothing.

`support.scaled_time_bound` is that allowance and nothing else. It takes the
bound a test already decided on and multiplies it by one factor read from
`AGENTIC_HIL_TEST_TIME_SCALE`, so a runner that knows it is loaded widens every
bound in the suite the same way with one variable, and every base value stays
in the test that chose it. With the variable unset the number is unchanged,
which is the property that keeps each test's claim the claim it already made.

A value that is not a positive number is refused by name. The alternative,
falling back to 1.0, would hand a runner that meant to widen its bounds and
mistyped the value the narrow ones back with no word about it, which is the
same silent rerun this issue is about. An exported but empty variable is the
one thing that is not a mistake: it is how a CI leg spells "not set", and it is
read as unset.

Two further values are positive numbers and are refused all the same, for the
one reason the refusal exists: a bound that stops saying anything, silently.

* Below 1.0 the factor narrows every bound in the suite instead of widening it,
  so a runner that meant to buy slack and typed `0.5` gets half the allowance
  the tests were written with and a screen of failures that name the code under
  test rather than the factor.
* At or above `TIME_SCALE_MAXIMUM`, and at infinity, every wall-clock assertion
  in the suite passes whatever the code does. `inf`, `infinity` and `1e9` all
  parse and all satisfy "positive", and a green suite that would be green with
  the product broken is worse than a red one.

So the accepted values are the finite numbers from `TIME_SCALE_MINIMUM` to
`TIME_SCALE_MAXIMUM` inclusive, and the message on everything else names the
variable, the value it was given and the two ends of that range, because the
reader is an operator looking at a runner's environment.

#522 is the rest of the suite. The helper landed in front of two bounds; every
other assertion that compares an elapsed measurement with a constant was left
with no slack a slower host can be granted, in every tier, and each of them has
turned a loaded run red with nothing wrong under it. So the last section here
walks the suite's own source and refuses a wall-clock ceiling that does not go
through the helper, the base values stay written where they were chosen, and
the two pyOCD phrase tests stop reporting a terminated stub as a stub that lost
pyOCD.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from support import TIME_SCALE_MAXIMUM, TIME_SCALE_MINIMUM, TIME_SCALE_VARIABLE, scaled_time_bound

TESTS = Path(__file__).resolve().parent
REPOSITORY_ROOT = TESTS.parent

# The bounds this issue is about, plus a sub-second one, so the helper is
# exercised on the numbers it exists for rather than on a round example.
BASE_BOUNDS = (5.0, 10.0, 1.0, 0.25)

# What the two named tests measured on the loaded machine in #515, and the base
# bounds they measured it against. These are the numbers the issue is about, so
# they are asserted rather than described: unset, both runs are still red, which
# is what keeps each test's claim the claim it made; under a modest factor both
# are green, which is what the variable is for.
PYOCD_BASE_BOUND_S = 5.0
MEASURED_PYOCD_ELAPSED_S = 5.07
HEARTBEAT_BASE_BOUND_S = 1.0
MEASURED_HEARTBEAT_AGE_S = 1.324


def test_the_variable_is_the_one_the_issue_named() -> None:
    """One name, spelled once, for the whole suite to read and a runner to set."""
    assert TIME_SCALE_VARIABLE == "AGENTIC_HIL_TEST_TIME_SCALE"


def test_the_accepted_range_has_both_ends_and_they_are_stated_once() -> None:
    """1.0 because the variable buys slack and never takes it away, and a ceiling
    because a factor large enough voids every bound in the suite in silence."""
    assert TIME_SCALE_MINIMUM == 1.0
    assert TIME_SCALE_MAXIMUM == 100.0


def test_an_unset_variable_leaves_every_bound_exactly_where_it_was(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default, and the reason no test's claim changes when this lands."""
    monkeypatch.delenv(TIME_SCALE_VARIABLE, raising=False)

    for base in BASE_BOUNDS:
        assert scaled_time_bound(base) == base, base


def test_an_exported_but_empty_variable_reads_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CI leg that forwards a variable nobody set forwards an empty string.

    Refusing it would fail the suite over a variable the runner never meant to
    use, so an empty value, and a value that is only whitespace, is the same as
    no value at all.
    """
    for empty in ("", "   ", "\t"):
        monkeypatch.setenv(TIME_SCALE_VARIABLE, empty)
        assert scaled_time_bound(5.0) == 5.0, repr(empty)


def test_the_factor_multiplies_the_bound_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three times the allowance, on each of the numbers the suite holds."""
    monkeypatch.setenv(TIME_SCALE_VARIABLE, "3")

    assert scaled_time_bound(5.0) == 15.0
    assert scaled_time_bound(10.0) == 30.0
    assert scaled_time_bound(1.0) == 3.0
    assert scaled_time_bound(0.25) == 0.75


def test_a_fractional_factor_is_read_as_a_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host that needs half again, not three times, says so in the same place."""
    monkeypatch.setenv(TIME_SCALE_VARIABLE, "1.5")

    assert scaled_time_bound(5.0) == 7.5
    assert scaled_time_bound(1.0) == 1.5


def test_a_factor_of_one_is_the_same_as_no_factor_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TIME_SCALE_VARIABLE, "1")

    for base in BASE_BOUNDS:
        assert scaled_time_bound(base) == base, base


def test_surrounding_whitespace_in_the_value_is_not_a_mistake(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shell that exported the factor with a space around it still means 3."""
    monkeypatch.setenv(TIME_SCALE_VARIABLE, "  3  ")

    assert scaled_time_bound(5.0) == 15.0


def test_an_integer_bound_comes_back_as_a_number_that_can_be_compared(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pyOCD file's base is the integer 5, and it is scaled like the rest."""
    monkeypatch.setenv(TIME_SCALE_VARIABLE, "2")

    assert scaled_time_bound(5) == 10.0
    assert scaled_time_bound(10) == 20.0


def test_the_factor_is_read_when_the_bound_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not once at import: a runner sets the variable for the process, and a
    test that changes it in flight, as the tests here do, has to see it."""
    monkeypatch.setenv(TIME_SCALE_VARIABLE, "2")
    assert scaled_time_bound(1.0) == 2.0

    monkeypatch.setenv(TIME_SCALE_VARIABLE, "4")
    assert scaled_time_bound(1.0) == 4.0

    monkeypatch.delenv(TIME_SCALE_VARIABLE)
    assert scaled_time_bound(1.0) == 1.0


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "0.0",
        "-1",
        "-0.5",
        "-3",
        "nonsense",
        "3x",
        "1,5",
        "nan",
        "None",
    ],
)
def test_a_factor_that_is_not_a_positive_number_is_refused_by_name(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Zero, a negative number and garbage all stop the run and say what to fix.

    The message carries the variable and the value it was given, because the
    reader is an operator looking at a runner's environment, not at this file.
    """
    monkeypatch.setenv(TIME_SCALE_VARIABLE, value)

    with pytest.raises(ValueError) as refused:
        scaled_time_bound(5.0)

    said = str(refused.value)
    assert TIME_SCALE_VARIABLE in said, said
    assert value in said, said


def test_a_refused_factor_never_falls_back_to_the_narrow_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction of the same rule, pinned so a later `except` cannot
    quietly turn a mistyped factor back into 1.0."""
    monkeypatch.setenv(TIME_SCALE_VARIABLE, "three")

    with pytest.raises(ValueError):
        scaled_time_bound(1.0)


@pytest.mark.parametrize(
    "value",
    [
        "inf",
        "+inf",
        "Infinity",
        "infinity",
        "1e9",
        "101",
        "100.5",
    ],
)
def test_a_factor_large_enough_to_void_every_bound_is_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """An infinite or absurd factor makes every wall-clock assertion in the suite
    true whatever the product does, which is a green run that means nothing.

    It is refused for the same reason a mistyped factor is: the failure a runner
    can act on is the one that names the variable, not a suite that stopped
    asserting.
    """
    monkeypatch.setenv(TIME_SCALE_VARIABLE, value)

    with pytest.raises(ValueError) as refused:
        scaled_time_bound(5.0)

    said = str(refused.value)
    assert TIME_SCALE_VARIABLE in said, said
    assert value in said, said
    assert str(TIME_SCALE_MAXIMUM) in said, said


@pytest.mark.parametrize("value", ["0.5", "0.999", "0.1"])
def test_a_factor_below_one_narrows_the_bounds_and_is_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """The variable exists to buy a loaded host slack, so taking slack away is
    never what the runner that set it meant.

    A positive number below 1.0 would tighten every bound in the suite at once
    and fail tests by name in the code under test, which is exactly the kind of
    red that teaches nothing, so it is refused where it can still be read.
    """
    monkeypatch.setenv(TIME_SCALE_VARIABLE, value)

    with pytest.raises(ValueError) as refused:
        scaled_time_bound(5.0)

    said = str(refused.value)
    assert TIME_SCALE_VARIABLE in said, said
    assert value in said, said
    assert str(TIME_SCALE_MINIMUM) in said, said


def test_both_ends_of_the_accepted_range_are_themselves_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inclusive at both ends, so the documented numbers are usable numbers."""
    monkeypatch.setenv(TIME_SCALE_VARIABLE, str(TIME_SCALE_MINIMUM))
    assert scaled_time_bound(5.0) == 5.0

    monkeypatch.setenv(TIME_SCALE_VARIABLE, str(TIME_SCALE_MAXIMUM))
    assert scaled_time_bound(5.0) == 5.0 * TIME_SCALE_MAXIMUM


# ---------------------------------------------------------------------------
# The two measurements the issue reports, asserted in both directions.


def test_with_no_factor_set_both_reported_runs_are_still_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """The claims are unchanged by this helper landing.

    5.07 s was over the 5.0 s bound before and is over it now, and a heartbeat
    read 1.324 s old still fails the one-second freshness bound. Nothing about
    the default widened anything.
    """
    monkeypatch.delenv(TIME_SCALE_VARIABLE, raising=False)

    assert scaled_time_bound(PYOCD_BASE_BOUND_S) < MEASURED_PYOCD_ELAPSED_S
    assert scaled_time_bound(HEARTBEAT_BASE_BOUND_S) < MEASURED_HEARTBEAT_AGE_S


def test_a_modest_factor_covers_both_runs_the_issue_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """And half again is enough for both, which is the point of the exercise: the
    reported failures were tenths of a second over, not a broken product."""
    monkeypatch.setenv(TIME_SCALE_VARIABLE, "1.5")

    assert scaled_time_bound(PYOCD_BASE_BOUND_S) > MEASURED_PYOCD_ELAPSED_S
    assert scaled_time_bound(HEARTBEAT_BASE_BOUND_S) > MEASURED_HEARTBEAT_AGE_S


def test_the_variable_is_documented_where_the_suite_is_run() -> None:
    """CONTRIBUTING.md is where this repository says how to run its own suite.

    `docs/testing.md` is the product's document about running hardware tests
    with the reactor and the pytest plugin, and this variable steers neither, so
    the sentence belongs beside `pytest -n auto` instead.
    """
    document = REPOSITORY_ROOT / "CONTRIBUTING.md"
    # CONTRIBUTING.md is repository content, not package content, so a source
    # distribution's checkout does not carry it. Skip there rather than fail, the
    # way test_generated_configurations_can_flash.py skips the statements it
    # reads out of docs/; every contributor and every CI leg runs this from a
    # repository checkout, where the document is always present.
    if not document.is_file():
        pytest.skip("CONTRIBUTING.md is repository content and does not ship in a source distribution")
    text = document.read_text(encoding="utf-8")
    assert TIME_SCALE_VARIABLE in text, "the suite's own document does not name the variable"

    paragraphs = [block for block in text.split("\n\n") if TIME_SCALE_VARIABLE in block]
    assert paragraphs, TIME_SCALE_VARIABLE
    said = "\n\n".join(paragraphs)
    assert "unset" in said, said
    assert str(TIME_SCALE_MINIMUM) in said, said
    assert str(int(TIME_SCALE_MAXIMUM)) in said or str(TIME_SCALE_MAXIMUM) in said, said


# ---------------------------------------------------------------------------
# The two bounds the issue names, taken through the helper with their base
# values unchanged. Read from the sources, the way the dash gate reads them,
# because with the factor unset both files behave exactly as they did before
# and nothing else would notice the helper being dropped again.


# Every way the two files compare a measured wall-clock number against a bound,
# with how many such comparisons each file holds. The rule is per line and
# positive, so it survives a rename of the base constant, a reformatting of the
# assertion and a revert written with the constant instead of the literal, none
# of which a forbidden-literal guard survives.
WALL_CLOCK_COMPARISONS = {
    "test_pyocd_without_a_probe.py": (('elapsed_ms"] <', "elapsed_s <"), 6),
    "test_bench_mutex.py": (('heartbeat_age_s"] <',), 2),
}


def test_every_wall_clock_comparison_in_the_two_files_is_taken_through_the_helper() -> None:
    """The shape that was red under load, gone from both files, and staying gone.

    A line that compares a measured duration against a bound has to call the
    helper on that line: that is what a revert would have to defeat on purpose
    rather than by writing the same assertion a slightly different way.
    """
    for name, (needles, expected) in WALL_CLOCK_COMPARISONS.items():
        found = 0
        for number, line in enumerate((TESTS / name).read_text(encoding="utf-8").splitlines(), start=1):
            if line.strip().startswith("#") or not any(needle in line for needle in needles):
                continue
            found += 1
            assert "scaled_time_bound(" in line, f"{name}:{number} compares against a bare bound: {line.strip()}"
        assert found == expected, f"{name} holds {found} wall-clock comparisons, not {expected}"


def test_the_two_bounds_the_issue_names_keep_their_base_values() -> None:
    """The allowance is configurable; the claim is not. Both base numbers stay
    written in the file that chose them, in one place each."""
    for name, base in (
        ("test_pyocd_without_a_probe.py", "CONFIGURED_TIMEOUT_S = 5"),
        ("test_bench_mutex.py", "FRESH_HEARTBEAT_AGE_S = 1.0"),
    ):
        text = (TESTS / name).read_text(encoding="utf-8")
        assert "scaled_time_bound(" in text, f"{name} mentions the helper without calling it"
        assert base in text, f"{name} no longer states its base bound as {base}"


# ---------------------------------------------------------------------------
# #522: the rest of the suite's wall-clock ceilings, in every tier, and the
# pyOCD stub that a loaded host was terminating instead of timing out.


# What this walk counts as a measured duration. The vocabulary is deliberately
# small and is the one the issue's own list is written in: `elapsed`,
# `elapsed_s`, `elapsed_ms` however they are spelled or subscripted, and the
# `_age_s` a heartbeat's age is read under. Every name in it is a wall-clock
# measurement and nothing else, which is what lets the walk be positive about
# each line it reports rather than arguable about it.
MEASUREMENT = re.compile(r"(?<![A-Za-z0-9_])(?:elapsed[A-Za-z0-9_]*|[A-Za-z0-9_]*_age_s)(?![A-Za-z0-9_])")


@dataclass(frozen=True)
class Ceiling:
    """One assertion line that bounds a measured duration from above."""

    path: Path
    number: int
    line: str

    @property
    def name(self) -> str:
        return self.path.relative_to(TESTS).as_posix()

    @property
    def where(self) -> str:
        return f"{self.name}:{self.number}"

    @property
    def through_the_helper(self) -> bool:
        return "scaled_time_bound(" in self.line


def ceilings_in(text: str, path: Path) -> list[Ceiling]:
    """Every line in ``text`` that bounds a measured duration from above.

    A line counts when three things are true of it at once: it is an assertion,
    it holds a ``<``, and one of the measurement words above stands to the left
    of that ``<``. The last part is what separates a ceiling from a floor, since
    a floor is written ``elapsed >= ...`` and holds no ``<`` at all, and a floor
    must never take this factor: widening it would have the suite demand that a
    loaded host be slower still.

    A regex over assertion lines, on purpose. What it catches is the shape the
    issue is about and the shape a revert would be written in, and what it does
    not catch is worth stating rather than pretending away:

    * a measurement named outside the vocabulary above, `waited` and `duration`
      among them, which the suite does use in a few places;
    * a ceiling written the other way round, ``BOUND > elapsed``, which the
      suite writes nowhere today;
    * an assertion spread over more than one physical line;
    * a bound applied outside an `assert`, a `pytest.approx` window, or a sleep
      chosen to match one;
    * a commented-out line, which is skipped deliberately: it asserts nothing.

    So this is a floor under the rule and not a proof of it. It fails on every
    line the issue lists, and it goes on failing if one of them comes back.
    """
    found: list[Ceiling] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line.startswith("assert "):
            continue
        head, separator, _ = line.partition("<")
        if not separator or not MEASUREMENT.search(head):
            continue
        found.append(Ceiling(path, number, line))
    return found


def walked_files() -> list[Path]:
    """Every Python file under `tests/`, which is every tier the suite has.

    The plain tier, the container tier and the bench tier are all read: a bound
    only a bench or only the image can reach is exactly the bound whose failure
    costs the most to diagnose, so none of them is exempt.
    """
    return sorted(TESTS.rglob("*.py"))


def suite_ceilings() -> list[Ceiling]:
    return [entry for path in walked_files() for entry in ceilings_in(path.read_text(encoding="utf-8"), path)]


# A file of the shape the walk reads, written as a list of strings so that no
# line of this module is itself an assertion the walk would then report.
SCANNER_SAMPLE = "\n".join(
    [
        "def sample() -> None:",
        '    assert elapsed < CALL_CEILING_S, "a bare ceiling, the shape this walk exists to find"',
        '    assert elapsed_s < scaled_time_bound(2.0), "the same ceiling, taken through the helper"',
        '    assert result["elapsed_ms"] < scaled_time_bound(5.0) * 1000, "a measurement read out of an envelope"',
        '    assert refusal["heartbeat_age_s"] < scaled_time_bound(1.0), "an age, a duration under another name"',
        '    assert elapsed >= FLOOR_S, "a floor, which this factor must never be allowed to move"',
        '    # assert elapsed < CALL_CEILING_S, "a line that asserts nothing"',
        '    assert waited < 2.0, "a measurement outside the vocabulary, and the walk says so"',
    ]
)


def test_the_walk_tells_a_bare_ceiling_from_a_scaled_one_and_leaves_a_floor_alone() -> None:
    """The scanner's own claim, on a sample holding one of each shape.

    Without this the walk could be narrowed to nothing and the pin below would
    stay green while the suite went back to bare bounds.
    """
    found = ceilings_in(SCANNER_SAMPLE, TESTS / "sample.py")

    assert [entry.number for entry in found] == [2, 3, 4, 5], [entry.line for entry in found]
    assert [entry.through_the_helper for entry in found] == [False, True, True, True], [entry.line for entry in found]


def test_the_walk_reads_every_tier_the_suite_has() -> None:
    """The plain tier, the container tier and the bench tier, all of them."""
    tiers = {("tests" if path.parent == TESTS else path.parent.name) for path in walked_files()}

    assert {"tests", "container", "bench"} <= tiers, sorted(tiers)


def test_every_wall_clock_ceiling_in_the_suite_is_taken_through_the_helper() -> None:
    """The pin the issue asks for: no ceiling on a measured duration without the factor.

    The failure lists the offenders by file and line, because the reader is
    somebody deciding what to change, not somebody deciding whether to rerun.
    """
    offenders = [entry for entry in suite_ceilings() if not entry.through_the_helper]

    assert not offenders, "wall-clock ceilings a loaded host cannot be granted any slack on:\n" + "\n".join(
        f"  {entry.where}: {entry.line}" for entry in offenders
    )


# How many such ceilings each file holds today. Asserted so the pin above cannot
# be made green by deleting the assertions instead of scaling them, and so a
# walk that quietly stopped reading a tier says so here.
CEILINGS_PER_FILE = {
    "bench/test_bench_coordination.py": 2,
    "container/test_debugger_processes_against_openocd.py": 2,
    "container/test_pyocd_without_a_probe.py": 1,
    "test_bench_mutex.py": 2,
    "test_com_stdio_bridge.py": 3,
    "test_debug_backend_refusals.py": 1,
    "test_debugger_processes.py": 4,
    "test_install_eval.py": 1,
    "test_pyocd_without_a_probe.py": 6,
    "test_reactor_runtime.py": 2,
    "test_redact.py": 2,
    "test_run_lifecycle.py": 2,
}


def test_the_ceilings_the_walk_finds_are_the_ones_the_suite_holds() -> None:
    """The count per file, unchanged by this issue: the fix scales them, it moves none.

    A file the walk expects and cannot find is not asserted about, because a
    source distribution ships only part of this tree (MANIFEST.in excludes the
    evaluation tests among others) and a from-sdist collection has to pass here
    too. What is not allowed is the tree shrinking to nothing unnoticed, so the
    number of files that did answer is asserted as well.
    """
    counted: dict[str, int] = {}
    for entry in suite_ceilings():
        counted[entry.name] = counted.get(entry.name, 0) + 1
    expected = {name: count for name, count in CEILINGS_PER_FILE.items() if (TESTS / name).is_file()}

    assert counted == expected, sorted(counted.items())
    assert len(expected) >= 10, sorted(expected)


# The base value behind each ceiling, stated where it was chosen. The factor is
# the runner's to set; the claim is the test author's, and this issue moves none
# of them. Named constants first.
BASE_CONSTANTS = {
    "bench/test_bench_coordination.py": ("REFUSAL_CEILING_S = 30.0",),
    "container/test_debugger_processes_against_openocd.py": ("CALL_CEILING_S = 15.0",),
    "container/test_pyocd_without_a_probe.py": ("TIMEOUT_S = 40",),
    "test_bench_mutex.py": ("FRESH_HEARTBEAT_AGE_S = 1.0",),
    "test_com_stdio_bridge.py": ("SHUTDOWN_CEILING_S = 1.5",),
    "test_debug_backend_refusals.py": ("START_TIMEOUT_S = 10.0",),
    "test_debugger_processes.py": ("CALL_CEILING_S = 15.0", "EMPTIED_GROUP_CEILING_S = 4.0"),
    "test_pyocd_without_a_probe.py": ("CONFIGURED_TIMEOUT_S = 5",),
}

# And the ones written as a literal on the assertion line itself, with how many
# of that file's ceiling lines have to carry each.
LITERAL_BASES = {
    "test_install_eval.py": (("20", 1),),
    "test_reactor_runtime.py": (("1.0", 1), ("WORKER_EXIT_GRACE_S + 3.0", 1)),
    "test_redact.py": (("5.0", 2),),
    "test_run_lifecycle.py": (("30", 1), ("60", 1)),
}


def test_the_named_base_constants_keep_the_values_they_had() -> None:
    """Every ceiling constant the issue lists, at the number it already stood at."""
    for name, bases in BASE_CONSTANTS.items():
        path = TESTS / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for base in bases:
            assert base in text, f"{name} no longer states its base bound as {base}"


def test_the_literal_bounds_keep_the_numbers_they_had() -> None:
    """The bounds written on the assertion line, still on it after the helper wraps them.

    `assert elapsed < 5.0` becoming `assert elapsed < scaled_time_bound(5.0)`
    keeps the 5.0 where the author put it, which is the whole property: the
    factor widens, it does not decide.
    """
    for name, bases in LITERAL_BASES.items():
        path = TESTS / name
        if not path.is_file():
            continue
        lines = [entry.line for entry in ceilings_in(path.read_text(encoding="utf-8"), path)]
        for base, expected in bases:
            carrying = [line for line in lines if base in line]
            assert len(carrying) == expected, f"{name}: {len(carrying)} of its ceilings carry {base}, not {expected}"


# ---------------------------------------------------------------------------
# The pyOCD stub that was being terminated rather than timing out.
#
# `tests/test_pyocd_unknown_target_phrases.py` runs a stub that is a Python
# process importing pyOCD twice, under the `timeout_s` its configuration writes.
# On a loaded host that budget runs out, the child is terminated, its stderr is
# empty, and both tests report a stub that stopped reaching pyOCD, which is the
# one failure the file exists to catch and is here the wrong answer. The log
# envelope says so all along: it carries `timed_out`, and neither test reads it.
#
# Two things follow, and both are pinned here. The configured budget takes the
# same factor as every bound above, so the test measures what it measured. And
# both tests read `timed_out` and name the factor when it is true, so a slow
# host is reported as slow.

PYOCD_PHRASES = TESTS / "test_pyocd_unknown_target_phrases.py"

# The two that run the stub through the service. The other tests in that file
# drive the installed pyOCD in this process and time nothing.
PYOCD_PHRASE_TESTS_THAT_RUN_THE_STUB = (
    "test_the_real_refusal_reaches_the_cmsis_pack_remediation",
    "test_the_stub_carries_the_installed_pyocd_wording",
)

# What `tests/conftest.py` writes today, and what the base has to stay.
STUB_BASE_TIMEOUT_S = 5.0


def function_body(text: str, name: str) -> str:
    """The source of one top-level function, from its `def` to the next one."""
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith(f"def {name}(")]
    assert len(starts) == 1, f"{name} is not defined exactly once in the file"
    start = starts[0]
    end = start + 1
    while end < len(lines) and not lines[end].startswith("def "):
        end += 1
    return "\n".join(lines[start:end])


def configured_timeout_s(config_path: Path) -> float:
    """The one `timeout_s` a written test configuration carries."""
    written = re.findall(r"timeout_s:\s*([0-9]+(?:\.[0-9]+)?)", config_path.read_text(encoding="utf-8"))
    assert len(written) == 1, f"{config_path} carries {len(written)} timeout_s entries: {written}"
    return float(written[0])


def test_both_pyocd_phrase_tests_read_the_envelope_s_timed_out() -> None:
    """A terminated stub is reported as a terminated stub, not as lost wording.

    The envelope already knows. What was missing is a test that asks it, and a
    message naming the variable an operator can act on, because "the stub did
    not reach pyOCD" sends the reader to the stub and to pyOCD, and the answer
    was neither.
    """
    text = PYOCD_PHRASES.read_text(encoding="utf-8")

    for name in PYOCD_PHRASE_TESTS_THAT_RUN_THE_STUB:
        body = function_body(text, name)
        assert "timed_out" in body, f"{name} never reads the log envelope's timed_out"
        assert TIME_SCALE_VARIABLE in body or "TIME_SCALE_VARIABLE" in body, (
            f"{name} can report a terminated stub without naming {TIME_SCALE_VARIABLE}"
        )


def test_the_stub_s_configured_timeout_follows_the_factor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget the stub runs under is scaled like every bound above it.

    Read through one seam, `stub_config`, the single helper both tests write
    their configuration with: a pin that only read the source could not tell a
    scaled value from the words `scaled_time_bound` sitting nearby, and the
    number on disk is the number the child actually runs under.

    Unset, the file still says 5, which is what keeps the test the test it was.
    """
    pytest.importorskip("pyocd", reason="pyOCD is an optional extra (install agentic-hil[pyocd])")
    phrases = importlib.import_module("test_pyocd_unknown_target_phrases")
    write = getattr(phrases, "stub_config", None)
    assert callable(write), "test_pyocd_unknown_target_phrases has no stub_config writing the stub's configuration"

    monkeypatch.delenv(TIME_SCALE_VARIABLE, raising=False)
    assert configured_timeout_s(write(tmp_path / "unset")) == STUB_BASE_TIMEOUT_S

    monkeypatch.setenv(TIME_SCALE_VARIABLE, "3")
    assert scaled_time_bound(STUB_BASE_TIMEOUT_S) == 15.0
    assert configured_timeout_s(write(tmp_path / "scaled")) == pytest.approx(scaled_time_bound(STUB_BASE_TIMEOUT_S))


def test_the_helper_ships_where_the_container_tier_can_import_it() -> None:
    """`tests/support.py` is package content, so the container tier and a source
    distribution both find it beside the tests that import it.

    The image copies this checkout in whole and the tier is a package under
    `tests/`, so the directory holding the helper is on the path there for the
    same reason it is here. What could still break it is an exclusion, so the
    exclusion list is what this reads.
    """
    manifest = REPOSITORY_ROOT / "MANIFEST.in"
    if not manifest.is_file():
        pytest.skip("MANIFEST.in is repository content and is read from a checkout")
    text = manifest.read_text(encoding="utf-8")

    assert "recursive-include tests *.py" in text, text
    excluded = [line for line in text.splitlines() if line.startswith(("exclude ", "prune ")) and "support" in line]
    assert not excluded, excluded
