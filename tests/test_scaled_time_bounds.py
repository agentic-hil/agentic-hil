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
"""

from __future__ import annotations

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
