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
"""

from __future__ import annotations

from pathlib import Path

import pytest
from support import TIME_SCALE_VARIABLE, scaled_time_bound

TESTS = Path(__file__).resolve().parent

# The bounds this issue is about, plus a sub-second one, so the helper is
# exercised on the numbers it exists for rather than on a round example.
BASE_BOUNDS = (5.0, 10.0, 1.0, 0.25)


def test_the_variable_is_the_one_the_issue_named() -> None:
    """One name, spelled once, for the whole suite to read and a runner to set."""
    assert TIME_SCALE_VARIABLE == "AGENTIC_HIL_TEST_TIME_SCALE"


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


# ---------------------------------------------------------------------------
# The two bounds the issue names, taken through the helper with their base
# values unchanged. Read from the sources, the way the dash gate reads them,
# because with the factor unset both files behave exactly as they did before
# and nothing else would notice the helper being dropped again.


def test_the_two_bounds_the_issue_names_are_taken_through_the_helper() -> None:
    for name, base in (
        ("test_pyocd_without_a_probe.py", "CONFIGURED_TIMEOUT_S = 5"),
        ("test_bench_mutex.py", "FRESH_HEARTBEAT_AGE_S = 1.0"),
    ):
        text = (TESTS / name).read_text(encoding="utf-8")
        assert "scaled_time_bound" in text, name
        assert base in text, f"{name} no longer states its base bound as {base}"


def test_neither_named_bound_is_compared_against_the_bare_base_any_more() -> None:
    """The shape that was red under load, gone from both files."""
    pyocd = (TESTS / "test_pyocd_without_a_probe.py").read_text(encoding="utf-8")
    assert 'elapsed_ms"] < CONFIGURED_TIMEOUT_S * 1000' not in pyocd
    assert "elapsed_s < 2 * CONFIGURED_TIMEOUT_S" not in pyocd

    mutex = (TESTS / "test_bench_mutex.py").read_text(encoding="utf-8")
    assert 'heartbeat_age_s"] < 1.0' not in mutex
