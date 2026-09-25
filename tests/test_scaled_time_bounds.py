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

import ast
import functools
import importlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

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
# small, and every name in it is a wall-clock measurement and nothing else,
# which is what lets the walk be positive about each line it reports rather than
# arguable about it: `elapsed` however it is spelled or suffixed, `waited` the
# same way, and the `_age_s` a heartbeat's age is read under.
MEASUREMENT = re.compile(r"(?<![A-Za-z0-9_])(?:elapsed[A-Za-z0-9_]*|waited[A-Za-z0-9_]*|[A-Za-z0-9_]*_age_s)(?![A-Za-z0-9_])")

# The one spelling a bound is allowed to reach the factor through.
HELPER = "scaled_time_bound("

# A statement is joined out of at most this many physical lines. A guard against
# a file whose brackets this reader cannot balance, never a limit any assertion
# in the suite comes near.
STATEMENT_LINE_LIMIT = 40


def without_string_contents(text: str) -> str:
    """`text` with the inside of every string literal and every comment blanked.

    The result has the same length as the input, so an index into one is an
    index into the other. What is inside a message is not code and must not be
    read as code: `f"a wait of {ASKED_WAIT_S:.0f}s (asked for)"` carries a
    bracket that closes nothing and a `<` in more than one failure message.
    """
    out: list[str] = []
    quote = ""
    escaped = False
    for char in text:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
                out.append(char)
                continue
            out.append(" ")
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            continue
        if char == "#":
            out.append(" " * (len(text) - len(out)))
            break
        out.append(char)
    return "".join(out)


def bracket_depth(masked: str) -> int:
    """How many brackets a masked line leaves open."""
    return sum(masked.count(char) for char in "([{") - sum(masked.count(char) for char in ")]}")


def assert_statements(text: str) -> list[tuple[int, int, str]]:
    """Every `assert` statement in `text`, as (first line, indentation, one line).

    A statement wrapped over several physical lines is joined into one, so a
    ceiling that was wrapped reads like any other and the walk below has no
    blind spot a reformatting could open. The indentation is the first line's,
    which is how an assertion nested inside an `if` is told from one that always
    runs.
    """
    lines = text.splitlines()
    statements: list[tuple[int, int, str]] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        if not raw.strip().startswith("assert "):
            index += 1
            continue
        start = index
        joined = raw.strip()
        while (
            bracket_depth(without_string_contents(joined)) > 0
            and index + 1 < len(lines)
            and index - start < STATEMENT_LINE_LIMIT
        ):
            index += 1
            joined = f"{joined} {lines[index].strip()}"
        statements.append((start + 1, len(raw) - len(raw.lstrip()), joined))
        index += 1
    return statements


def first_comparison(masked: str) -> tuple[int, str] | None:
    """Where a masked statement first compares with `<`, `<=`, `>` or `>=`."""
    depth = 0
    for index, char in enumerate(masked):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0 and char in "<>":
            return index, char + ("=" if masked[index + 1 : index + 2] == "=" else "")
    return None


def bound_expression(statement: str, masked: str, start: int) -> str:
    """The expression a comparison holds its measurement against.

    Everything after the operator up to the comma that begins the assertion's
    message, which is what separates the bound from the prose about it. A
    message naming the helper therefore proves nothing here.
    """
    depth = 0
    for index in range(start, len(masked)):
        char = masked[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            return statement[start:index].strip()
    return statement[start:].strip()


def split_helper_call(bound: str) -> tuple[str, str] | None:
    """`(what the helper was handed, what stands after it)`, or None.

    A bound goes through the factor when the helper is the first thing in it, so
    the base is what the factor multiplies and anything outside the call is a
    unit conversion applied afterwards. `scaled_time_bound(A) * 1000` scales A;
    `A + scaled_time_bound(B)` scales half of a bound and is refused.
    """
    if not bound.startswith(HELPER):
        return None
    masked = without_string_contents(bound)
    depth = 0
    for index, char in enumerate(masked):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return bound[len(HELPER) : index].strip(), bound[index + 1 :].strip()
    return None


@dataclass(frozen=True)
class Comparison:
    """One assertion that holds a measured duration against a bound."""

    path: Path
    number: int
    indentation: int
    statement: str
    operator: str
    bound: str

    @property
    def name(self) -> str:
        return self.path.relative_to(TESTS).as_posix()

    @property
    def where(self) -> str:
        return f"{self.name}:{self.number}"

    @property
    def is_ceiling(self) -> bool:
        """A ceiling bounds the measurement from above; a floor from below.

        The distinction decides everything here. A ceiling has to take the
        factor, and a floor must never be allowed to: widening a floor would
        have the suite demand that a loaded host be slower still.
        """
        return self.operator.startswith("<")

    @property
    def through_the_helper(self) -> bool:
        return split_helper_call(self.bound) is not None

    @property
    def base(self) -> str:
        """The bound with the factor taken back off it, which is the number or
        expression the test author chose and which this issue does not move."""
        split = split_helper_call(self.bound)
        return split[0] if split else self.bound

    @property
    def outside_the_helper(self) -> str:
        split = split_helper_call(self.bound)
        return split[1] if split else ""


def comparisons_in(text: str, path: Path) -> list[Comparison]:
    """Every assertion in `text` that compares a measured duration with a bound.

    A statement counts when its first top-level comparison is `<`, `<=`, `>` or
    `>=` and one of the measurement words above stands to the left of it.

    A reader of assertion lines, on purpose. What it catches is the shape the
    issue is about and the shape a revert would be written in, and what it does
    not catch is worth stating rather than pretending away:

    * a measurement named outside the vocabulary above, `duration` among them,
      which the suite happens not to use for a wall-clock reading today;
    * a bound written the other way round, `BOUND > elapsed`, which the suite
      writes nowhere;
    * a bound applied outside an `assert`, a `pytest.approx` window, or a sleep
      chosen to match one;
    * a commented-out line, which is skipped deliberately: it asserts nothing.

    So this is a floor under the rule and not a proof of it. It fails on every
    line the issue lists, and it goes on failing if one of them comes back.
    """
    found: list[Comparison] = []
    for number, indentation, statement in assert_statements(text):
        masked = without_string_contents(statement)
        position = first_comparison(masked)
        if position is None:
            continue
        index, operator = position
        # The measurement is looked for in the statement itself and not in the
        # masked copy, because `result["elapsed_ms"]` keeps its name inside a
        # subscript that masking blanks out. Everything to the left of the
        # operator is the measured side, so a message cannot reach it.
        if not MEASUREMENT.search(statement[:index]):
            continue
        found.append(
            Comparison(
                path=path,
                number=number,
                indentation=indentation,
                statement=statement,
                operator=operator,
                bound=bound_expression(statement, masked, index + len(operator)),
            )
        )
    return found


def walked_files() -> list[Path]:
    """Every Python file under `tests/`, which is every tier the suite has.

    The plain tier, the container tier and the bench tier are all read: a bound
    only a bench or only the image can reach is exactly the bound whose failure
    costs the most to diagnose, so none of them is exempt.
    """
    return sorted(TESTS.rglob("*.py"))


# The comparisons the walk finds that are not bounds on this host's speed, and
# are therefore not this issue's to widen. Each is named by a substring of the
# statement rather than by a line number, and each has to go on matching exactly
# one statement, so an exemption cannot quietly outlive the assertion it was
# written for or grow to cover a second one.
#
# `test_run_lifecycle.py` holds the first. `step["waited_ms"] < 600_000` reads a
# number the product wrote into its own step envelope and holds it against the
# ten minutes the plan asked for, so the claim is that a stop cut the wait
# short. The product answers that the same way on a fast host and a slow one; a
# factor there would loosen a product claim while granting a loaded host
# nothing, which is the opposite of what this issue is for.
#
# `test_sessions_devices_coordination.py` holds the second (#530).
# `elapsed < BROKER_REFUSAL_CEILING_S` is half `canbroker.BROKER_START_TIMEOUT_S`
# and its whole claim is that the refusal came from the broker's own exit code
# rather than from the client's deadline running out. Scaling it would let the
# factor carry the ceiling up to that deadline and past it, at which point the
# line stops telling the two apart and a green run says nothing; the number it
# already grants a loaded host is nine times what the refusal costs.
#
# `test_can_broker_deadline.py` holds the third, and it is the same kind of
# claim (#532). `broker.waited_for` is the list of timeout arguments the client
# handed a process double whose clock is a fake, so the sum is the budget the
# product asked for and not one second this host spent; the ceiling is the
# envelope that makes the shutdown budget a short one. A factor there would let
# the product's own budget grow on a slow host, which is the envelope inverted.
EXEMPT_BOUNDS = {
    "test_run_lifecycle.py": ('step["waited_ms"] < 600_000',),
    "test_sessions_devices_coordination.py": ("elapsed < BROKER_REFUSAL_CEILING_S",),
    "test_can_broker_deadline.py": ("sum(broker.waited_for) <= GRACE_CEILING_S",),
}


def is_exempt(entry: Comparison | TimeoutSite) -> bool:
    return any(needle in entry.statement for needle in EXEMPT_BOUNDS.get(entry.name, ()))


def suite_comparisons() -> list[Comparison]:
    return [entry for path in walked_files() for entry in comparisons_in(path.read_text(encoding="utf-8"), path)]


def suite_ceilings() -> list[Comparison]:
    return [entry for entry in suite_comparisons() if entry.is_ceiling and not is_exempt(entry)]


def suite_floors() -> list[Comparison]:
    return [entry for entry in suite_comparisons() if not entry.is_ceiling]


# A file of the shape the walk reads, written as a list of strings so that no
# line of this module is itself an assertion the walk would then report.
SCANNER_SAMPLE = "\n".join(
    [
        "def sample() -> None:",
        '    assert elapsed < CALL_CEILING_S, "a bare ceiling, the shape this walk exists to find"',
        '    assert elapsed_s < scaled_time_bound(2.0), "the same ceiling, taken through the helper"',
        '    assert result["elapsed_ms"] < scaled_time_bound(5.0) * 1000, "a bound converted to milliseconds after scaling"',
        '    assert refusal["heartbeat_age_s"] < scaled_time_bound(1.0), "an age, a duration under another name"',
        '    assert waited < 2.0, "a measurement the first vocabulary missed"',
        "    assert waited_s < scaled_time_bound(timeout_s), (",
        '        f"a ceiling wrapped over two lines, and a bound that is a parameter: {waited_s}"',
        "    )",
        '    assert elapsed >= FLOOR_S, "a floor, which this factor must never be allowed to move"',
        '    assert step["waited_ms"] < 600_000, "a number out of the product\'s own envelope"',
        '    # assert elapsed < CALL_CEILING_S, "a line that asserts nothing"',
        '    assert ASKED_WAIT_S + 1 < 5, "no measurement on the left, so not this walk\'s business"',
    ]
)


def sample_comparisons() -> list[Comparison]:
    return comparisons_in(SCANNER_SAMPLE, TESTS / "sample.py")


def test_the_walk_finds_every_comparison_in_the_sample_and_nothing_else() -> None:
    """The scanner's own claim, on a sample holding one of each shape.

    Without this the walk could be narrowed to nothing and the pin below would
    stay green while the suite went back to bare bounds.
    """
    found = sample_comparisons()

    assert [entry.number for entry in found] == [2, 3, 4, 5, 6, 7, 10, 11], [entry.statement for entry in found]
    assert [entry.is_ceiling for entry in found] == [True, True, True, True, True, True, False, True]


def test_the_walk_tells_a_bare_ceiling_from_a_scaled_one() -> None:
    """And reads the base back off a scaled one, which is what pins the numbers."""
    by_line = {entry.number: entry for entry in sample_comparisons()}

    assert not by_line[2].through_the_helper
    assert by_line[2].base == "CALL_CEILING_S"
    assert by_line[3].through_the_helper and by_line[3].base == "2.0"
    assert by_line[4].through_the_helper and by_line[4].base == "5.0"
    assert by_line[4].outside_the_helper == "* 1000"
    assert by_line[7].through_the_helper and by_line[7].base == "timeout_s"


def test_a_message_that_names_the_helper_does_not_count_as_scaling() -> None:
    """The bound ends at the comma. What the failure says about itself is prose."""
    pretending = "\n".join(
        [
            "def sample() -> None:",
            '    assert elapsed < 5.0, f"set the factor, scaled_time_bound(5.0) would have allowed it"',
        ]
    )

    found = comparisons_in(pretending, TESTS / "sample.py")

    assert [entry.bound for entry in found] == ["5.0"], [entry.statement for entry in found]
    assert [entry.through_the_helper for entry in found] == [False], [entry.bound for entry in found]


def test_only_the_helper_at_the_front_of_a_bound_counts() -> None:
    """Half a composite bound scaled is not a scaled bound, and says so here."""
    half = "\n".join(
        [
            "def sample() -> None:",
            "    assert elapsed < ASKED_WAIT_S + scaled_time_bound(REFUSAL_CEILING_S), elapsed",
        ]
    )

    found = comparisons_in(half, TESTS / "sample.py")

    assert [entry.through_the_helper for entry in found] == [False], [entry.bound for entry in found]


def test_the_walk_reads_every_tier_the_suite_has() -> None:
    """The plain tier, the container tier and the bench tier, all of them."""
    tiers = {("tests" if path.parent == TESTS else path.parent.name) for path in walked_files()}

    assert {"tests", "container", "bench"} <= tiers, sorted(tiers)


def test_every_wall_clock_ceiling_in_the_suite_is_taken_through_the_helper() -> None:
    """The pin the issue asks for: no ceiling on a measured duration without the factor.

    The failure lists the offenders by file and line, because the reader is
    somebody deciding what to change, not somebody deciding whether to rerun.
    """
    walk = TimeoutWalk(TESTS)
    offenders = [entry for entry in suite_ceilings() if not entry.through_the_helper and not ceiling_takes_the_factor(entry, walk)]

    assert not offenders, "wall-clock ceilings a loaded host cannot be granted any slack on:\n" + "\n".join(
        f"  {entry.where}: {entry.statement}" for entry in offenders
    )


def test_no_floor_in_the_suite_takes_the_factor() -> None:
    """The other half of the rule, and the mistake the sweep makes available.

    Several of the ceilings this issue widens sit one line under a floor on the
    same measurement, `elapsed >= ASKED_WAIT_S - WAIT_SHORTFALL_S` among them. A
    factor on one of those would have the suite demand that a loaded host be
    slower still, and would be green on exactly the machines it is wrong on.
    """
    scaled = [entry for entry in suite_floors() if HELPER in entry.bound]

    assert not scaled, "floors widened by a factor that only ever grants a host more time:\n" + "\n".join(
        f"  {entry.where}: {entry.statement}" for entry in scaled
    )


# What may stand after the helper call in a bound. Spelled once, because the
# timeout walk at the end of this file gives a timeout the verdict this gives a
# ceiling.
UNIT_CONVERSION = re.compile(r"^(?:[*/]\s*[0-9][0-9_.]*)?$")


def test_nothing_outside_a_helper_call_is_part_of_a_bound() -> None:
    """What may stand after the call is a unit conversion, and nothing else.

    `scaled_time_bound(CONFIGURED_TIMEOUT_S) * 1000` is the same bound in
    milliseconds. `scaled_time_bound(A) + B` is half a bound, and it is the
    shape a sweep produces when it wraps the first name it meets.
    """
    outside = [entry for entry in suite_ceilings() if not UNIT_CONVERSION.match(entry.outside_the_helper)]

    assert not outside, "bounds with something other than a unit conversion outside the factor:\n" + "\n".join(
        f"  {entry.where}: {entry.bound}" for entry in outside
    )


def test_every_exemption_still_names_one_comparison_the_walk_finds() -> None:
    """An exemption that stopped matching is an exemption nobody is reading.

    A file the walk cannot find is not asserted about, because a source
    distribution ships only part of this tree.
    """
    for name, needles in EXEMPT_BOUNDS.items():
        if not (TESTS / name).is_file():
            continue
        statements = [entry.statement for entry in suite_comparisons() if entry.name == name]
        statements += [site.statement for site in suite_timeout_sites() if site.name == name]
        for needle in needles:
            matching = [statement for statement in statements if needle in statement]
            assert len(matching) == 1, f"{name}: {len(matching)} comparisons carry {needle!r}, not one"


# How many such ceilings each file holds. A minimum rather than an exact count:
# the property worth having is that the pin above cannot be made green by
# deleting the assertions instead of scaling them, and that a walk which quietly
# stopped reading a tier says so here. An exact count would add to that only the
# guarantee that no legitimate, already scaled ceiling is ever added anywhere in
# the suite without editing this map, which is friction bought with nothing.
CEILINGS_PER_FILE = {
    "bench/test_bench_coordination.py": 2,
    "test_can_broker_deadline.py": 1,
    "container/test_can_over_vcan.py": 2,
    "container/test_debugger_processes_against_openocd.py": 2,
    "container/test_pyocd_without_a_probe.py": 1,
    "test_bench_mutex.py": 2,
    "test_com_stdio_bridge.py": 3,
    "test_debug_backend_refusals.py": 1,
    "test_debugger_processes.py": 4,
    "test_devices.py": 1,
    "test_install_eval.py": 1,
    "test_pyocd_without_a_probe.py": 6,
    "test_reactor_runtime.py": 2,
    "test_redact.py": 2,
    "test_run_lifecycle.py": 4,
}


def test_every_file_still_holds_the_ceilings_it_held() -> None:
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

    missing = {name: (counted.get(name, 0), count) for name, count in expected.items() if counted.get(name, 0) < count}
    assert not missing, f"files holding fewer wall-clock ceilings than they did: {missing}"
    assert len(expected) >= 10, sorted(expected)


# The base behind each ceiling, spelled as it stands in the file that chose it.
# The factor is the runner's to set; the claim is the test author's, and this
# issue moves none of them. Read back through the helper, so a composite bound
# has to hand the factor the whole of what it had: `ASKED_WAIT_S +
# REFUSAL_CEILING_S` scaled in one half is not this bound, and the map says so
# once here rather than being argued three times during the sweep.
BASE_EXPRESSIONS = {
    "bench/test_bench_coordination.py": {"REFUSAL_CEILING_S": 1, "ASKED_WAIT_S + REFUSAL_CEILING_S": 1},
    "test_can_broker_deadline.py": {"REAL_ATTACH_DEADLINE_S + GRACE_CEILING_S": 1},
    "container/test_can_over_vcan.py": {"BUS_TIMEOUT_S + WAIT_SLACK_S": 1, "BUS_TIMEOUT_S + WAIT_SLACK_S + 2.0": 1},
    "container/test_debugger_processes_against_openocd.py": {"CALL_CEILING_S": 2},
    "container/test_pyocd_without_a_probe.py": {"TIMEOUT_S / 2": 1},
    "test_bench_mutex.py": {"FRESH_HEARTBEAT_AGE_S": 2},
    "test_com_stdio_bridge.py": {"SHUTDOWN_CEILING_S": 3},
    "test_debug_backend_refusals.py": {"START_TIMEOUT_S * 1000 / 2": 1},
    "test_debugger_processes.py": {"CALL_CEILING_S": 3, "EMPTIED_GROUP_CEILING_S": 1},
    "test_devices.py": {"2.0": 1},
    "test_install_eval.py": {"20": 1},
    "test_pyocd_without_a_probe.py": {"CONFIGURED_TIMEOUT_S": 3, "2 * CONFIGURED_TIMEOUT_S": 3},
    "test_reactor_runtime.py": {"runlifecycle.WORKER_EXIT_GRACE_S + 3.0": 1, "1.0": 1},
    "test_redact.py": {"5.0": 2},
    "test_run_lifecycle.py": {"30": 1, "60": 1, "timeout_s": 2},
}

# And the numbers those names stand for, at the value they already had, in the
# file that stated them. `timeout_s` is a poll helper's parameter rather than a
# constant, so its default is what is pinned. So is the `timeout` default of the
# two script runners, which take the factor where they hand it to the child.
BASE_CONSTANTS = {
    "bench/test_bench_coordination.py": ("REFUSAL_CEILING_S = 30.0", "ASKED_WAIT_S = 3.0"),
    "test_can_broker_deadline.py": ("GRACE_CEILING_S = 3.0", "REAL_ATTACH_DEADLINE_S = 0.5"),
    "container/test_can_over_vcan.py": ("BUS_TIMEOUT_S = 2.0", "WAIT_SLACK_S = 1.5"),
    "container/test_debugger_processes_against_openocd.py": ("CALL_CEILING_S = 15.0",),
    "container/test_pyocd_without_a_probe.py": ("TIMEOUT_S = 40",),
    "test_bench_mutex.py": ("FRESH_HEARTBEAT_AGE_S = 1.0",),
    "test_com_stdio_bridge.py": ("SHUTDOWN_CEILING_S = 1.5",),
    "test_debug_backend_refusals.py": ("START_TIMEOUT_S = 10.0",),
    "test_debugger_processes.py": ("CALL_CEILING_S = 15.0", "EMPTIED_GROUP_CEILING_S = 4.0"),
    "test_pyocd_without_a_probe.py": ("CONFIGURED_TIMEOUT_S = 5",),
    "test_run_lifecycle.py": ("timeout_s: float = 60.0",),
    "test_install_scripts.py": ("timeout: float = 180",),
    "test_windows_install_eval_scripts.py": ("timeout: int = 180",),
}


def test_every_ceiling_hands_the_factor_the_base_it_already_had() -> None:
    """`assert elapsed < 5.0` becomes `assert elapsed < scaled_time_bound(5.0)`.

    The 5.0 stays where the author put it, which is the whole property: the
    factor widens, it does not decide. Written per file as the expression the
    helper is handed, so it holds equally before and after the sweep and pins
    which half of a composite bound the factor takes.
    """
    for name, expressions in BASE_EXPRESSIONS.items():
        path = TESTS / name
        if not path.is_file():
            continue
        bases = [entry.base for entry in comparisons_in(path.read_text(encoding="utf-8"), path) if entry.is_ceiling]
        for expression, expected in expressions.items():
            carrying = [base for base in bases if base == expression]
            assert len(carrying) == expected, f"{name}: {len(carrying)} ceilings are bounded by {expression!r}, not {expected}"


def test_the_named_base_constants_keep_the_values_they_had() -> None:
    """Every ceiling constant the issue lists, at the number it already stood at."""
    for name, bases in BASE_CONSTANTS.items():
        path = TESTS / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for base in bases:
            assert base in text, f"{name} no longer states its base bound as {base}"


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
# Three things follow, and all three are pinned here. The configured budget
# takes the same factor as every bound above, so the test measures what it
# measured. Both tests fail on `timed_out` with a message naming the factor, so
# a slow host is reported as slow: a failure and not a skip, because a file that
# stops asserting on exactly the hosts this issue is about would be green with
# the product broken, which is the state #515 refuses a large factor for. And
# the wording claims stay unconditional, so reading the flag cannot become a way
# of not making them.

PYOCD_PHRASES = TESTS / "test_pyocd_unknown_target_phrases.py"

# The two that run the stub through the service. The other tests in that file
# drive the installed pyOCD in this process and time nothing.
PYOCD_PHRASE_TESTS_THAT_RUN_THE_STUB = (
    "test_the_real_refusal_reaches_the_cmsis_pack_remediation",
    "test_the_stub_carries_the_installed_pyocd_wording",
)

# The claim each of those two exists to make, which reading the flag must not
# turn into something a run can decline to assert.
PYOCD_UNCONDITIONAL_CLAIMS = {
    "test_the_real_refusal_reaches_the_cmsis_pack_remediation": '"target_type_invalid"',
    "test_the_stub_carries_the_installed_pyocd_wording": "real_pyocd_refusal()",
}

# What `tests/conftest.py` writes today, and what the base has to stay.
STUB_BASE_TIMEOUT_S = 5.0


def top_level_functions(text: str) -> list[str]:
    return [line[4 : line.index("(")] for line in text.splitlines() if line.startswith("def ") and "(" in line]


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


def reachable_source(text: str, name: str) -> str:
    """One test's own body plus the bodies of the module helpers it calls.

    The natural home for reading `timed_out` is the helper both tests already
    parse the log in, so a pin that only read a test's own body would refuse the
    right implementation. One level deep is enough for this module and keeps the
    reader something a person can follow.
    """
    own = function_body(text, name)
    bodies = [own]
    for helper in top_level_functions(text):
        if helper == name or helper.startswith("test_"):
            continue
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(helper)}\s*\(", own):
            bodies.append(function_body(text, helper))
    return "\n".join(bodies)


def configured_timeout_s(config_path: Path) -> float:
    """The one `timeout_s` a written test configuration carries."""
    written = re.findall(r"(?<![a-z_])timeout_s:\s*([0-9]+(?:\.[0-9]+)?)", config_path.read_text(encoding="utf-8"))
    assert len(written) == 1, f"{config_path} carries {len(written)} timeout_s entries: {written}"
    return float(written[0])


def stub_configuration_seam(module: ModuleType) -> Callable[[Path], Path]:
    """The module-level callable that writes the stub's configuration.

    Found rather than named: any callable defined in that module whose own name
    says it makes a configuration counts, so the test pins the seam's existence
    and its behaviour and leaves the spelling to the file. `write_config` and
    `load_config` are imported from elsewhere and are excluded by that.
    """
    candidates = [
        value
        for name, value in vars(module).items()
        if callable(value)
        and "config" in name
        and not name.startswith("test_")
        and getattr(value, "__module__", "") == getattr(module, "__name__", "")
    ]
    assert len(candidates) == 1, (
        f"test_pyocd_unknown_target_phrases exposes {len(candidates)} callables writing the stub's configuration, not one"
    )
    return candidates[0]


def test_both_pyocd_phrase_tests_fail_a_terminated_stub_by_name() -> None:
    """A terminated stub is reported as a slow host, not as lost pyOCD wording.

    The envelope already knows. What was missing is an assertion that asks it,
    and a message naming the variable an operator can act on, because "the stub
    did not reach pyOCD" sends the reader to the stub and to pyOCD, and the
    answer was neither.

    Read off the assertion lines rather than the whole body, so a docstring
    explaining `timed_out` cannot stand in for reading it, and refusing a skip,
    so the file cannot stop asserting on exactly the hosts this is about.
    """
    text = PYOCD_PHRASES.read_text(encoding="utf-8")

    for name in PYOCD_PHRASE_TESTS_THAT_RUN_THE_STUB:
        source = reachable_source(text, name)
        assert "pytest.skip" not in source, f"{name} skips rather than fails when its stub was terminated"
        carrying = [statement for _, _, statement in assert_statements(source) if "timed_out" in statement]
        assert carrying, f"{name} never asserts on the log envelope's timed_out"
        assert any(TIME_SCALE_VARIABLE in statement for statement in carrying), (
            f"{name} can report a terminated stub without naming {TIME_SCALE_VARIABLE}: {carrying}"
        )


def test_the_pyocd_wording_claims_stay_unconditional() -> None:
    """Reading the flag must not turn the file's own claims into an option.

    Each of the two keeps its claim at the body's own indentation, which is
    where an assertion that always runs stands and where one nested in an `if`
    does not.
    """
    text = PYOCD_PHRASES.read_text(encoding="utf-8")

    for name, claim in PYOCD_UNCONDITIONAL_CLAIMS.items():
        body = function_body(text, name)
        carrying = [(indentation, statement) for _, indentation, statement in assert_statements(body) if claim in statement]
        assert carrying, f"{name} no longer asserts {claim}"
        assert all(indentation == 4 for indentation, _ in carrying), f"{name} asserts {claim} conditionally: {carrying}"


def test_the_stub_s_configured_timeout_follows_the_factor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget the stub runs under is scaled like every bound above it.

    Read off the file the seam writes rather than out of its source, because the
    number the child actually runs under is the one on disk and a source pin
    could not tell a scaled value from the words `scaled_time_bound` standing
    nearby.

    Unset, the file still says 5, which is what keeps the test the test it was.
    """
    pytest.importorskip("pyocd", reason="pyOCD is an optional extra (install agentic-hil[pyocd])")
    phrases = importlib.import_module("test_pyocd_unknown_target_phrases")
    write = stub_configuration_seam(phrases)

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


# ---------------------------------------------------------------------------
# #533: the same rule for a budget a test hands to a call as its `timeout`.
#
# The walk above reads assertions, so a wall-clock budget written as a keyword
# never reaches it. `subprocess.run(..., timeout=30)` bounds this host's speed
# exactly as `assert elapsed < 30` does, and a loaded host turns it red the same
# way, with TimeoutExpired where the other raises AssertionError. A keyword, and
# the module-level definition of a name standing in one, are not things a
# physical line can be trusted to show, so this second walk reads the parse
# tree, where a string or a comment is not code in the first place.
#
# It reads, anywhere under `tests/`: the `timeout` keyword of `subprocess.run`,
# `call`, `check_call` and `check_output`, under whatever name the module or the
# function was imported as; the `timeout` keyword or the first positional
# argument of any `.wait(...)`, whatever the receiver, because an `Event.wait(5)`
# spends this host's time as much as a child's wait does; the `timeout` keyword
# of any `.communicate(...)`; and the budget of a `pytest.mark.timeout` marker.
# Every other keyword on those calls is an input the test chose for the product
# (`timeout_s`, `wait_s`, `start_timeout_s`, a bus timeout) and is not read.
#
# A budget takes the factor when it is a helper call with nothing but a unit
# conversion after it, `None`, or a name (or a module's attribute) whose
# module-level definition is such a call, an import from another file of the
# suite followed to that definition. A constant is therefore scaled once where it
# is defined, not at each use. The same rule spelled as a function is a call with
# no arguments of a module-level function that has no parameters and whose body,
# a docstring aside, is one `return` of such a call, and the assertion walk
# accepts that call as well. A local, a parameter or any other expression gets
# the verdict the assertion walk gives the same text. A timeout that is itself
# the subject of a test, a child that outlives it on purpose, is registered in
# EXEMPT_BOUNDS by a substring of its call, with its reason, the way a product
# envelope is.

SUBPROCESS_CALLS = ("run", "call", "check_call", "check_output")


def goes_through_the_helper(bound: str) -> bool:
    """The verdict the assertion walk gives a ceiling spelled `bound`."""
    split = split_helper_call(bound)
    return split is not None and UNIT_CONVERSION.match(split[1]) is not None


def ceiling_takes_the_factor(entry: Comparison, walk: TimeoutWalk) -> bool:
    """The verdict the assertion walk gives a ceiling in a file under the
    walk's root: a helper call with at most a unit conversion after it, or a
    call of a function that only returns one, read the way the timeout walk
    reads the same call, with the locals of the function the ceiling is in."""
    if goes_through_the_helper(entry.bound):
        return True
    try:
        bound = ast.parse(entry.bound, mode="eval").body
    except SyntaxError:
        return False
    if not isinstance(bound, ast.Call):
        return False
    definitions = walk.resolved(entry.path, bound, walk.enclosing_functions(entry.path, entry.number))
    return bool(definitions) and all(goes_through_the_helper(text) for text in definitions)


@dataclass(frozen=True)
class TimeoutSite:
    """One budget a test hands to a subprocess call, a wait or a timeout marker.

    The budget and the definitions behind it are held in the parser's own
    spelling, so a comment inside a wrapped expression, or where its lines
    break, cannot change the verdict on it.
    """

    name: str
    number: int
    callee: str
    value: str
    by_keyword: bool
    statement: str
    definitions: tuple[str, ...]

    @property
    def where(self) -> str:
        return f"{self.name}:{self.number}"

    @property
    def spelled(self) -> str:
        call = f"{self.callee}(timeout={self.value})" if self.by_keyword else f"{self.callee}({self.value})"
        return f"{call}, defined as {' and '.join(self.definitions)}" if self.definitions else call

    @property
    def takes_the_factor(self) -> bool:
        """A helper call, `None`, a name every module-level definition of which
        is a helper call, or a call of a function that only returns one. `None`
        is no budget at all, so there is nothing for a factor to widen."""
        if self.value == "None" or goes_through_the_helper(self.value):
            return True
        return bool(self.definitions) and all(goes_through_the_helper(text) for text in self.definitions)


def dotted_name(node: ast.expr) -> str | None:
    """`a.b.c` for a chain of attributes over a name, and None for anything else."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(parts)])


def call_spellings(imports: Iterable[ast.Import | ast.ImportFrom]) -> dict[str, str]:
    """Each spelling under which a file reaches a subprocess call or the timeout
    marker, mapped to the call it reaches.

    Handed every import in the file at any depth, so a function that imports
    `subprocess` for itself is read like a module that imports it at the top.
    """
    spellings: dict[str, str] = {}
    for node in imports:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    spellings.update({f"{alias.asname or alias.name}.{call}": f"subprocess.{call}" for call in SUBPROCESS_CALLS})
                elif alias.name == "pytest":
                    spellings[f"{alias.asname or alias.name}.mark.timeout"] = "pytest.mark.timeout"
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                if node.module == "subprocess" and alias.name in SUBPROCESS_CALLS:
                    spellings[alias.asname or alias.name] = f"subprocess.{alias.name}"
                elif node.module == "pytest" and alias.name == "mark":
                    spellings[f"{alias.asname or alias.name}.timeout"] = "pytest.mark.timeout"
    return spellings


def budget_of(call: ast.Call, spellings: dict[str, str]) -> tuple[ast.expr, bool] | None:
    """The budget a call is handed and whether it came by keyword, or None when
    the call is none of the shapes read or is handed no budget."""
    dotted = dotted_name(call.func)
    if dotted is not None and dotted in spellings:
        shape = spellings[dotted]
    elif isinstance(call.func, ast.Attribute) and call.func.attr in ("wait", "communicate"):
        shape = f".{call.func.attr}"
    else:
        return None
    for keyword in call.keywords:
        if keyword.arg == "timeout":
            return keyword.value, True
    if shape in (".wait", "pytest.mark.timeout") and call.args and not isinstance(call.args[0], ast.Starred):
        return call.args[0], False
    return None


def source_of(lines: list[str], node: ast.AST) -> str:
    """The text of `node` as the file spells it, its lines joined with single
    spaces the way the assertion walk joins a wrapped assertion. A registration
    is matched against this, so it can be copied out of the file.

    Offsets are counted in UTF-8 bytes, which is how the parser reports them.
    """
    encoded = [line.encode("utf-8") for line in lines[node.lineno - 1 : node.end_lineno]]
    encoded[-1] = encoded[-1][: node.end_col_offset]
    encoded[0] = encoded[0][node.col_offset :]
    return " ".join(text for text in (piece.decode("utf-8").strip() for piece in encoded) if text)


def first_line(lines: list[str], statement: ast.stmt) -> str:
    return lines[statement.lineno - 1].strip()


def module_scope_statements(body: list[ast.stmt]) -> list[ast.stmt]:
    """Every statement that runs in the module's own scope: the top level and
    whatever an `if`, `try`, `with`, loop or `match` there holds, but nothing
    inside a function or a class."""
    found: list[ast.stmt] = []
    for statement in body:
        found.append(statement)
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody"):
            found += module_scope_statements(getattr(statement, field, []))
        for nested in (*getattr(statement, "handlers", []), *getattr(statement, "cases", [])):
            found += module_scope_statements(nested.body)
    return found


def names_bound_by(statement: ast.stmt) -> set[str]:
    """The names one statement binds where it runs. The statements nested in its
    body are read on their own."""
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {statement.name}
    if isinstance(statement, (ast.Import, ast.ImportFrom)):
        return {alias.asname or alias.name.split(".")[0] for alias in statement.names if alias.name != "*"}
    targets: list[ast.expr] = []
    if isinstance(statement, ast.Assign):
        targets = statement.targets
    elif isinstance(statement, (ast.AugAssign, ast.For, ast.AsyncFor)) or (
        isinstance(statement, ast.AnnAssign) and statement.value is not None
    ):
        targets = [statement.target]
    elif isinstance(statement, (ast.With, ast.AsyncWith)):
        targets = [item.optional_vars for item in statement.items if item.optional_vars is not None]
    return {node.id for target in targets for node in ast.walk(target) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}


def names_a_function_binds(function: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> frozenset[str]:
    """Every name local to a function: its parameters and everything its body
    binds.

    Read over the whole body, nested functions included, and without honouring
    `global`, which can only make a name local that is not. A local is judged
    like any other expression, so the error is on the strict side.
    """
    arguments = function.args
    names = {argument.arg for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)}
    names.update(argument.arg for argument in (arguments.vararg, arguments.kwarg) if argument is not None)
    for statement in function.body if isinstance(function.body, list) else [function.body]:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                names.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                names.add(node.name)
            elif isinstance(node, ast.stmt):
                names.update(names_bound_by(node))
    return frozenset(names)


def returned_by(statement: ast.stmt) -> ast.expr | None:
    """What a function returns when it is the constant rule spelled as a
    function: no parameters, no decorator, not a coroutine, and a body that, a
    docstring aside, is one `return` of a value. None for any other statement.
    """
    if not isinstance(statement, ast.FunctionDef) or statement.decorator_list:
        return None
    arguments = statement.args
    if arguments.posonlyargs or arguments.args or arguments.kwonlyargs or arguments.vararg or arguments.kwarg:
        return None
    body = statement.body[1:] if ast.get_docstring(statement, clean=False) is not None else statement.body
    if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
        return None
    return body[0].value


class TimeoutWalk:
    """The budgets handed to the calls read, in every Python file under a root.

    The root is `tests/` for the pin, and a directory of synthetic sources for
    the tests that pin the walk itself.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.parsed: dict[Path, tuple[ast.Module, list[str]]] = {}
        self.bound: dict[Path, dict[str, list[ast.stmt]]] = {}
        self.function_locals: dict[ast.AST, frozenset[str]] = {}

    def module(self, path: Path) -> tuple[ast.Module, list[str]]:
        """A file's parse tree and lines. A byte order mark is not code, and a
        file that does not parse fails the walk by name instead of going unread."""
        if path not in self.parsed:
            text = path.read_text(encoding="utf-8-sig")
            self.parsed[path] = (ast.parse(text, filename=str(path)), text.split("\n"))
        return self.parsed[path]

    def bindings(self, path: Path) -> dict[str, list[ast.stmt]]:
        """Each name bound in a file's module scope, with the statements binding it."""
        if path not in self.bound:
            bound: dict[str, list[ast.stmt]] = {}
            for statement in module_scope_statements(self.module(path)[0].body):
                for name in names_bound_by(statement):
                    bound.setdefault(name, []).append(statement)
            self.bound[path] = bound
        return self.bound[path]

    def locals_of(self, function: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> frozenset[str]:
        if function not in self.function_locals:
            self.function_locals[function] = names_a_function_binds(function)
        return self.function_locals[function]

    def module_file(self, importer: Path, dotted: str, level: int) -> Path | None:
        """The file under the root an import names, or None for a module elsewhere.

        A relative import counts from the importing file's package. An absolute
        one is looked for from the root, which is on the path the way `tests/` is
        when the suite runs, and from above it when it names the root itself
        (`from tests.container.conftest import ...`).
        """
        parts = dotted.split(".")
        if level:
            bases = [importer.parents[level - 1]]
        elif parts[0] == self.root.name:
            bases = [self.root, self.root.parent]
        else:
            bases = [self.root]
        for base in bases:
            for candidate in (base.joinpath(*parts[:-1], f"{parts[-1]}.py"), base.joinpath(*parts, "__init__.py")):
                if candidate.is_file() and candidate.resolve().is_relative_to(self.root):
                    return candidate.resolve()
        return None

    def definitions(self, path: Path, name: str, seen: frozenset[tuple[Path, str]] = frozenset(), called: bool = False) -> tuple[str, ...]:
        """The text of every module-level definition of `name` in `path`, an
        import from another file under the root followed to the definition there.
        For a name the budget `called`, the definition is what the function
        returns, when it is a function `returned_by` reads.

        Any other binding, and an import that cannot be followed, comes back as
        its own first line, which no helper call starts, so it is refused rather
        than guessed at.
        """
        if (path, name) in seen:
            return ()
        lines = self.module(path)[1]
        texts: list[str] = []
        for statement in self.bindings(path).get(name, []):
            targets = statement.targets if isinstance(statement, ast.Assign) else [getattr(statement, "target", None)]
            if called and (returned := returned_by(statement)) is not None:
                texts.append(ast.unparse(returned))
            elif not called and isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None and any(isinstance(target, ast.Name) and target.id == name for target in targets):
                texts.append(ast.unparse(statement.value))
            elif isinstance(statement, ast.ImportFrom) and statement.module:
                imported = next(alias.name for alias in statement.names if (alias.asname or alias.name) == name)
                target = self.module_file(path, statement.module, statement.level)
                followed = self.definitions(target, imported, seen | {(path, name)}, called) if target else ()
                texts.extend(followed or [first_line(lines, statement)])
            else:
                texts.append(first_line(lines, statement))
        return tuple(texts)

    def attribute_definitions(self, path: Path, module: str, attribute: str, called: bool = False) -> tuple[str, ...]:
        """The definitions of `module.attribute`, when `module` is bound once in
        the file, by an import of a file under the root."""
        statements = self.bindings(path).get(module, [])
        if len(statements) != 1:
            return ()
        statement = statements[0]
        target: Path | None = None
        if isinstance(statement, ast.Import):
            target = next((self.module_file(path, alias.name, 0) for alias in statement.names if (alias.asname or alias.name) == module), None)
        elif isinstance(statement, ast.ImportFrom):
            target = next(
                (
                    self.module_file(path, f"{statement.module}.{alias.name}" if statement.module else alias.name, statement.level)
                    for alias in statement.names
                    if (alias.asname or alias.name) == module
                ),
                None,
            )
        return self.definitions(target, attribute, called=called) if target else ()

    def resolved(self, path: Path, value: ast.expr, functions: tuple[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, ...]) -> tuple[str, ...]:
        """The module-level definitions behind a budget that is a name or a
        module's attribute, or a call of one with no arguments. A local or a
        parameter has none, whatever its name, and neither has any other
        expression."""
        called = isinstance(value, ast.Call) and not value.args and not value.keywords
        named = value.func if isinstance(value, ast.Call) and called else value
        base = named.value if isinstance(named, ast.Attribute) else named
        if not isinstance(base, ast.Name) or any(base.id in self.locals_of(function) for function in functions):
            return ()
        if isinstance(named, ast.Attribute):
            return self.attribute_definitions(path, base.id, named.attr, called)
        return self.definitions(path, base.id, called=called)

    def enclosing_functions(self, path: Path, number: int) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]:
        """The functions whose bodies hold line `number` of a file, so a ceiling
        there is read with the locals a budget there is read with."""
        return tuple(
            node
            for node in ast.walk(self.module(path)[0])
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.body[0].lineno <= number <= (node.end_lineno or 0)
        )

    def sites_in(self, path: Path) -> list[TimeoutSite]:
        tree, lines = self.module(path)
        imports: list[ast.Import | ast.ImportFrom] = []
        calls: list[tuple[ast.Call, tuple[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, ...]]] = []
        pending: list[tuple[ast.AST, tuple[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, ...]]] = [(tree, ())]
        while pending:
            node, functions = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                # Decorators and defaults are evaluated where the function is
                # defined, and the body in a scope of its own.
                outer = [*getattr(node, "decorator_list", []), *node.args.defaults, *node.args.kw_defaults]
                pending.extend((expression, functions) for expression in outer if expression is not None)
                body = node.body if isinstance(node.body, list) else [node.body]
                pending.extend((statement, (*functions, node)) for statement in body)
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports.append(node)
            elif isinstance(node, ast.Call):
                calls.append((node, functions))
            pending.extend((child, functions) for child in ast.iter_child_nodes(node))
        # A call is judged once every import is known, since one below it in
        # the file can still name what it reaches.
        spellings = call_spellings(imports)
        found: list[tuple[int, int, TimeoutSite]] = []
        for node, functions in calls:
            if (budget := budget_of(node, spellings)) is None:
                continue
            value, by_keyword = budget
            site = TimeoutSite(
                name=path.relative_to(self.root).as_posix(),
                number=value.lineno,
                callee=source_of(lines, node.func),
                value=ast.unparse(value),
                by_keyword=by_keyword,
                statement=source_of(lines, node),
                definitions=self.resolved(path, value, functions),
            )
            found.append((value.lineno, value.col_offset, site))
        return [site for _, _, site in sorted(found, key=lambda entry: entry[:2])]

    def sites(self) -> list[TimeoutSite]:
        return [site for path in sorted(self.root.rglob("*.py")) for site in self.sites_in(path)]


def timeout_sites_under(root: Path) -> list[TimeoutSite]:
    """Every budget handed to a call the walk reads, in every Python file under `root`."""
    return TimeoutWalk(root).sites()


@functools.cache
def suite_timeout_sites() -> tuple[TimeoutSite, ...]:
    """The suite's own sites, read once per process, because the whole tree is parsed."""
    return tuple(timeout_sites_under(TESTS))


def refused_timeouts(sites: Iterable[TimeoutSite]) -> list[TimeoutSite]:
    return [site for site in sites if not site.takes_the_factor and not is_exempt(site)]


def write_sources(root: Path, sources: dict[str, list[str]], encoding: str = "utf-8") -> None:
    """Lay out synthetic files under `root`, each written from a list of lines
    for the reason SCANNER_SAMPLE is."""
    for name, lines in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding=encoding)


def marked(lines: list[str], marker: str) -> list[int]:
    """The numbers of the lines in a sample whose trailing comment is `marker`."""
    return [number for number, line in enumerate(lines, start=1) if line.endswith(f"# {marker}")]


# One of each call shape the issue names, under each way of importing it, and in
# each place a test puts one.
SHAPES_SAMPLE = [
    "import subprocess",
    "import subprocess as sp",
    "from subprocess import check_output as output_of",
    "from subprocess import run",
    "",
    'subprocess.run(["true"], timeout=30)  # refused',
    "",
    "",
    "def test_every_shape(child, event) -> None:",
    '    subprocess.call(["true"], timeout=31)  # refused',
    '    subprocess.check_call(["true"], timeout=32)  # refused',
    '    subprocess.check_output(["true"], timeout=33)  # refused',
    '    sp.run(["true"], timeout=34)  # refused',
    '    run(["true"], timeout=35)  # refused',
    '    output_of(["true"], timeout=36)  # refused',
    "    child.wait(timeout=15)  # refused",
    "    child.wait(16)  # refused",
    "    event.wait(timeout=5)  # refused",
    '    subprocess.Popen(["true"]).wait(timeout=17)  # refused',
    "    child.communicate(timeout=10)  # refused",
    '    child.communicate(b"input", timeout=11)  # refused',
    "    subprocess.run(",
    '        ["true"],',
    "        capture_output=True,",
    "        timeout=37,  # refused",
    "    )",
    "",
    "",
    "class Holder:",
    "    def stop(self) -> None:",
    "        self.process.wait(18)  # refused",
    "",
    "",
    "async def test_in_a_coroutine(child) -> None:",
    "    child.wait(timeout=19)  # refused",
    "",
    "",
    "def test_in_a_helper(child) -> None:",
    "    def stop() -> None:",
    "        child.wait(timeout=20)  # refused",
    "",
    "    stop()",
    "    closing = lambda: child.communicate(timeout=21)  # refused",
    "    closing()",
]


def test_each_call_shape_with_a_bare_budget_is_refused_by_file_and_line(tmp_path: Path) -> None:
    """Every shape the issue names, however it was imported, wherever a test
    puts it: module level, a test, a method, a coroutine, a nested helper and a
    lambda. The receiver of a wait is not typed, so an event's wait is read like
    a child's. The line named is the one the budget stands on."""
    write_sources(tmp_path, {"sample/test_shapes.py": SHAPES_SAMPLE})

    refused = refused_timeouts(timeout_sites_under(tmp_path))

    expected = marked(SHAPES_SAMPLE, "refused")
    assert len(expected) == 18
    assert [site.where for site in refused] == [f"sample/test_shapes.py:{number}" for number in expected], [
        site.spelled for site in refused
    ]


ACCEPTED_ROOT_CONFTEST = [
    "from support import scaled_time_bound",
    "",
    "INSTALL_TIMEOUT_S = scaled_time_bound(600.0)",
]

ACCEPTED_TIER_CONFTEST = [
    "from support import scaled_time_bound",
    "",
    "COMMAND_TIMEOUT_S = scaled_time_bound(300.0)",
]

ACCEPTED_SAMPLE = [
    "import subprocess",
    "",
    "from conftest import INSTALL_TIMEOUT_S",
    "from support import scaled_time_bound",
    "",
    "from . import conftest",
    "from .conftest import COMMAND_TIMEOUT_S",
    "",
    "CHILD_TIMEOUT_S = scaled_time_bound(30)",
    "",
    "",
    "def test_accepted(child, event) -> None:",
    '    subprocess.run(["true"], timeout=scaled_time_bound(30))  # accepted',
    "    child.wait(scaled_time_bound(15))  # accepted",
    "    child.communicate(timeout=None)  # accepted",
    "    event.wait(timeout=CHILD_TIMEOUT_S)  # accepted",
    '    subprocess.check_call(["true"], timeout=COMMAND_TIMEOUT_S)  # accepted',
    "    child.wait(timeout=conftest.COMMAND_TIMEOUT_S)  # accepted",
    '    subprocess.run(["true"], timeout=INSTALL_TIMEOUT_S)  # accepted',
    "    child.wait(",
    "        timeout=scaled_time_bound(  # accepted",
    "            30,",
    "        ),",
    "    )",
]


def test_each_accepted_form_of_a_budget_passes(tmp_path: Path) -> None:
    """A helper call, `None`, and a name or a module's attribute defined as a
    helper call, in this file or in a conftest reached the way each tier imports
    its own. A helper call wrapped over lines reads like one that is not."""
    write_sources(
        tmp_path,
        {
            "conftest.py": ACCEPTED_ROOT_CONFTEST,
            "accepted/conftest.py": ACCEPTED_TIER_CONFTEST,
            "accepted/test_accepted.py": ACCEPTED_SAMPLE,
        },
    )

    found = timeout_sites_under(tmp_path)

    assert [site.where for site in found] == [f"accepted/test_accepted.py:{number}" for number in marked(ACCEPTED_SAMPLE, "accepted")]
    assert not refused_timeouts(found), [site.spelled for site in refused_timeouts(found)]


REGISTERED_SAMPLE = [
    "import subprocess",
    "",
    "import pytest",
    "",
    "OUTLIVED_TIMEOUT_S = 0.5",
    "",
    "",
    "def test_a_child_that_outlives_its_budget(child) -> None:",
    "    with pytest.raises(subprocess.TimeoutExpired):",
    '        subprocess.run(["sleep", "60"], timeout=OUTLIVED_TIMEOUT_S)  # registered',
    "    child.wait(timeout=30)  # refused",
]


def test_a_deliberate_timeout_is_registered_the_way_a_product_envelope_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A budget that is itself the question, a child that outlives it on
    purpose, is named in EXEMPT_BOUNDS by a substring of its call and passes.
    Scaling it would change what the test asks. The file's other budgets are
    still read."""
    write_sources(tmp_path, {"registered/test_registered.py": REGISTERED_SAMPLE})
    sites = timeout_sites_under(tmp_path)
    registered, refused = marked(REGISTERED_SAMPLE, "registered"), marked(REGISTERED_SAMPLE, "refused")

    assert [site.number for site in refused_timeouts(sites)] == registered + refused

    needle = "timeout=OUTLIVED_TIMEOUT_S"
    monkeypatch.setitem(EXEMPT_BOUNDS, "registered/test_registered.py", (needle,))

    assert [site.number for site in sites if needle in site.statement] == registered
    assert [site.number for site in refused_timeouts(sites)] == refused


CONSTANTS_TIER_CONFTEST = [
    "INSTALL_TIMEOUT_S = 600.0",
]

CONSTANTS_SAMPLE = [
    "import subprocess",
    "",
    "from support import scaled_time_bound",
    "",
    "from .conftest import INSTALL_TIMEOUT_S",
    "",
    "CHILD_TIMEOUT_S = scaled_time_bound(30)",
    "BARE_TIMEOUT_S = 30",
    "",
    "",
    "def test_constants(child) -> None:",
    "    child.wait(timeout=CHILD_TIMEOUT_S)  # accepted",
    "    child.wait(CHILD_TIMEOUT_S)  # accepted",
    "    child.wait(timeout=BARE_TIMEOUT_S)  # refused",
    '    subprocess.run(["true"], timeout=INSTALL_TIMEOUT_S)  # refused',
    "",
    "",
    "def test_a_parameter_is_not_the_constant(child, CHILD_TIMEOUT_S=30) -> None:",
    "    child.wait(timeout=CHILD_TIMEOUT_S)  # refused",
    "",
    "",
    "def test_a_local_is_not_the_constant(child) -> None:",
    "    CHILD_TIMEOUT_S = 30",
    "    child.wait(timeout=CHILD_TIMEOUT_S)  # refused",
]


def test_a_module_constant_is_judged_once_at_its_definition(tmp_path: Path) -> None:
    """`CHILD_TIMEOUT_S = scaled_time_bound(30)` scales every use of the name,
    and a constant defined as a bare number is refused at each use, the line a
    reader converting it is sent to, with the definition named beside it. An
    import is followed to the file that defines the constant. A parameter or a
    local that shares the constant's name is not the constant."""
    write_sources(tmp_path, {"constants/conftest.py": CONSTANTS_TIER_CONFTEST, "constants/test_constants.py": CONSTANTS_SAMPLE})

    found = timeout_sites_under(tmp_path)

    accepted, refused = marked(CONSTANTS_SAMPLE, "accepted"), marked(CONSTANTS_SAMPLE, "refused")
    assert [site.number for site in found] == sorted(accepted + refused)
    assert [site.number for site in refused_timeouts(found)] == refused
    by_line = {site.number: site for site in found}
    assert by_line[12].definitions == ("scaled_time_bound(30)",)
    assert by_line[14].definitions == ("30",)
    assert by_line[15].definitions == ("600.0",)
    assert by_line[19].definitions == by_line[24].definitions == ()


# Locals and computed expressions, each written once as a ceiling and once as a
# budget, so the two walks are asked about the same text.
MIRRORED_BOUNDS = (
    "30",
    "budget",
    "timeout_s",
    "2 * CHILD_TIMEOUT_S",
    "scaled_time_bound(30) * 1000",
    "scaled_time_bound(30) + 5",
    "5 + scaled_time_bound(30)",
    "scaled_time_bound(LONG_S) if slow else scaled_time_bound(SHORT_S)",
    "max(scaled_time_bound(30), 1.0)",
    "wait_bound()",
)


def test_a_local_or_computed_budget_gets_the_verdict_the_assertion_walk_gives(tmp_path: Path) -> None:
    """The policy mirrored, not loosened: a name standing for a scaled module
    constant is the one addition, and only where it stands alone. A call of a
    function that returns a helper call and does nothing else is accepted by
    both walks."""
    lines = [
        "from support import scaled_time_bound",
        "",
        "CHILD_TIMEOUT_S = scaled_time_bound(30)",
        "",
        "",
        "def wait_bound() -> float:",
        "    return scaled_time_bound(30)",
        "",
        "",
        "def sample(child, budget, timeout_s, slow, LONG_S, SHORT_S) -> None:",
    ]
    for bound in MIRRORED_BOUNDS:
        lines.append(f"    assert elapsed < {bound}, 'a ceiling'")
        lines.append(f"    child.wait(timeout={bound})")
    write_sources(tmp_path, {"mirror/test_mirror.py": lines})
    walk = TimeoutWalk(tmp_path)

    ceilings = comparisons_in("\n".join(lines), tmp_path / "mirror" / "test_mirror.py")
    by_the_assertion_walk = [ceiling_takes_the_factor(entry, walk) for entry in ceilings]
    by_the_timeout_walk = [site.takes_the_factor for site in timeout_sites_under(tmp_path)]

    assert by_the_timeout_walk == by_the_assertion_walk, list(zip(MIRRORED_BOUNDS, by_the_timeout_walk, strict=False))
    assert by_the_assertion_walk == [bound in ("scaled_time_bound(30) * 1000", "wait_bound()") for bound in MIRRORED_BOUNDS]


# The constant rule spelled as a function, beside each way a function can fail
# to be it. Each call is written once as a ceiling and once as a budget, for the
# reason the mirrored bounds above are.
FUNCTION_TIER_CONFTEST = [
    "from support import scaled_time_bound",
    "",
    "",
    "def install_bound() -> float:",
    "    return scaled_time_bound(600.0)",
]

FUNCTION_DEFINITIONS = [
    "import functools",
    "",
    "from support import scaled_time_bound",
    "",
    "from . import conftest",
    "from .conftest import install_bound",
    "",
    "WAIT_TIMEOUT_S = 5.0",
    "",
    "",
    "def wait_bound() -> float:",
    '    """The module\'s one wait ceiling."""',
    "    return scaled_time_bound(WAIT_TIMEOUT_S)",
    "",
    "",
    "def wait_bound_ms() -> float:",
    "    return scaled_time_bound(WAIT_TIMEOUT_S) * 1000",
    "",
    "",
    "def bound_for(timeout_s: float = WAIT_TIMEOUT_S) -> float:",
    "    return scaled_time_bound(timeout_s)",
    "",
    "",
    "def logged_bound() -> float:",
    '    print("waiting")',
    "    return scaled_time_bound(WAIT_TIMEOUT_S)",
    "",
    "",
    "def bare_bound() -> float:",
    "    return WAIT_TIMEOUT_S",
    "",
    "",
    "def padded_bound() -> float:",
    "    return scaled_time_bound(WAIT_TIMEOUT_S) + 1.0",
    "",
    "",
    "@functools.cache",
    "def cached_bound() -> float:",
    "    return scaled_time_bound(WAIT_TIMEOUT_S)",
    "",
    "",
    "async def awaited_bound() -> float:",
    "    return scaled_time_bound(WAIT_TIMEOUT_S)",
    "",
    "",
    "def shadowed_bound() -> float:",
    "    return scaled_time_bound(WAIT_TIMEOUT_S)",
    "",
    "",
    "def sample(child, shadowed_bound) -> None:",
]

# Each call, with the verdict both walks owe it.
FUNCTION_BOUNDS = {
    "wait_bound()": True,
    "wait_bound_ms()": True,
    "install_bound()": True,
    "conftest.install_bound()": True,
    "wait_bound": False,
    "bound_for()": False,
    "bound_for(WAIT_TIMEOUT_S)": False,
    "logged_bound()": False,
    "bare_bound()": False,
    "padded_bound()": False,
    "cached_bound()": False,
    "awaited_bound()": False,
    "shadowed_bound()": False,
}


def test_a_function_that_only_returns_a_scaled_bound_takes_the_factor_in_both_walks(tmp_path: Path) -> None:
    """The constant rule spelled as a function. A call with no arguments of a
    module-level function that has no parameters, and whose body, a docstring
    aside, is one `return` of a helper call, takes the factor in both walks, in
    its own file or through an import. A parameter, another statement, a return
    of anything else, a decorator or a coroutine is refused, and so is a local
    that shares the function's name, and the function itself uncalled."""
    lines = list(FUNCTION_DEFINITIONS)
    for bound in FUNCTION_BOUNDS:
        lines.append(f"    assert elapsed < {bound}, 'a ceiling'")
        lines.append(f"    child.wait(timeout={bound})")
    write_sources(tmp_path, {"functions/conftest.py": FUNCTION_TIER_CONFTEST, "functions/test_functions.py": lines})
    walk = TimeoutWalk(tmp_path)

    ceilings = comparisons_in("\n".join(lines), tmp_path / "functions" / "test_functions.py")
    by_the_assertion_walk = [ceiling_takes_the_factor(entry, walk) for entry in ceilings]
    by_the_timeout_walk = [site.takes_the_factor for site in timeout_sites_under(tmp_path)]

    expected = list(FUNCTION_BOUNDS.values())
    assert by_the_timeout_walk == expected, list(zip(FUNCTION_BOUNDS, by_the_timeout_walk, strict=False))
    assert by_the_assertion_walk == expected, list(zip(FUNCTION_BOUNDS, by_the_assertion_walk, strict=False))


MARKER_SAMPLE = [
    "import pytest",
    "from pytest import mark",
    "from support import scaled_time_bound",
    "",
    "pytestmark = pytest.mark.timeout(3)  # refused",
    "",
    "",
    "@pytest.mark.timeout(3)  # refused",
    "def test_positional() -> None:",
    "    pass",
    "",
    "",
    "@pytest.mark.timeout(timeout=4)  # refused",
    "def test_keyword() -> None:",
    "    pass",
    "",
    "",
    "@mark.timeout(5)  # refused",
    "def test_imported_mark() -> None:",
    "    pass",
    "",
    "",
    "@pytest.mark.timeout(scaled_time_bound(3))  # accepted",
    "def test_scaled() -> None:",
    "    pass",
]


def test_a_bare_number_in_a_pytest_timeout_marker_is_refused(tmp_path: Path) -> None:
    """None exist in the suite. The rule stands anyway, on the same grounds: the
    marker's budget is this host's time like any other."""
    write_sources(tmp_path, {"marker/test_marker.py": MARKER_SAMPLE})

    found = timeout_sites_under(tmp_path)

    refused = marked(MARKER_SAMPLE, "refused")
    assert [site.number for site in found] == sorted(refused + marked(MARKER_SAMPLE, "accepted"))
    assert [site.number for site in refused_timeouts(found)] == refused


INPUTS_SAMPLE = [
    "import subprocess",
    "",
    "",
    "def test_inputs(service, session, bus, child, config) -> None:",
    "    service.wait(timeout_s=4.0)",
    "    session.wait(wait_s=2.0, interval_ms=50)",
    '    attach_participant(config, "bench", "alpha", start_timeout_s=4.0)',
    "    bus.recv(timeout=0.5)",
    '    subprocess.run(["true"], env={"TIMEOUT": "30"})',
    '    subprocess.Popen(["true"], text=True)',
    "    child.wait()",
    '    child.communicate(b"input")',
    "    child.wait(timeout=12)  # read",
]


def test_no_keyword_but_timeout_is_read_on_those_calls(tmp_path: Path) -> None:
    """A number the test hands the product as an input stays the test's choice:
    `timeout_s`, `wait_s`, `interval_ms`, `start_timeout_s` and a bus's own
    timeout are not read, nor is the input a child is handed. The last line is
    the control that shows the walk was reading."""
    write_sources(tmp_path, {"inputs/test_inputs.py": INPUTS_SAMPLE})

    found = timeout_sites_under(tmp_path)

    assert [site.number for site in found] == marked(INPUTS_SAMPLE, "read"), [site.spelled for site in found]


PROSE_SAMPLE = [
    "import subprocess",
    "",
    'CHILD = """',
    "import subprocess",
    'subprocess.run(["true"], timeout=30)',
    '"""',
    "",
    "",
    "def test_prose(child) -> None:",
    '    """Calls `child.wait(timeout=30)` and never `subprocess.run(timeout=5)`."""',
    "    # child.wait(timeout=30)",
    '    said = "child.communicate(timeout=10)"',
    '    raw = b"child.wait(5)"',
    '    noted = f"{said} child.wait(timeout=30)"',
    "    child.wait(timeout=None)  # not child.wait(timeout=30)  # read",
    '    subprocess.run(["python", "-c", "import time; time.sleep(1)"], check=False)',
]


def test_strings_and_comments_are_not_read_as_budgets(tmp_path: Path) -> None:
    """A child's script, a docstring, a comment and a message are not code, and
    nothing in them is a budget; the one real call beside them is read."""
    write_sources(tmp_path, {"prose/test_prose.py": PROSE_SAMPLE})

    found = timeout_sites_under(tmp_path)

    assert [(site.number, site.value) for site in found] == [(number, "None") for number in marked(PROSE_SAMPLE, "read")]
    assert not refused_timeouts(found)


def test_a_file_that_opens_with_a_byte_order_mark_is_read_like_any_other(tmp_path: Path) -> None:
    """Two files of the suite start with one, and the parser refuses the
    character when it is read as plain UTF-8, so a walk that did that would lose
    exactly those files."""
    lines = ["import subprocess", "", "", "def test_marked() -> None:", '    subprocess.run(["true"], timeout=30)  # refused']
    write_sources(tmp_path, {"bom/test_bom.py": lines}, encoding="utf-8-sig")

    assert (tmp_path / "bom" / "test_bom.py").read_bytes().startswith(b"\xef\xbb\xbf")
    assert [site.where for site in refused_timeouts(timeout_sites_under(tmp_path))] == ["bom/test_bom.py:5"]


def test_a_file_that_does_not_parse_fails_the_walk_by_name(tmp_path: Path) -> None:
    """Never a file skipped in silence: a tree the walk could not read is a tree
    it cannot vouch for."""
    write_sources(tmp_path, {"broken/test_broken.py": ["def test_broken(:", "    pass"]})

    with pytest.raises(SyntaxError) as refused:
        timeout_sites_under(tmp_path)

    assert str(refused.value.filename).endswith("test_broken.py"), refused.value


def test_the_timeout_walk_reads_every_tier_the_suite_has() -> None:
    """The plain tier, the container tier and the bench tier, all of them."""
    tiers = {site.name.split("/")[0] if "/" in site.name else "tests" for site in suite_timeout_sites()}

    assert {"tests", "container", "bench"} <= tiers, sorted(tiers)


def test_every_budget_the_suite_hands_a_call_takes_the_factor() -> None:
    """The pin the issue asks for: no timeout on a child, a wait or a marker
    that a loaded host cannot be granted slack on.

    The failure lists each by file and line with the call as it is spelled and
    the definition behind a constant, because the reader is deciding what to
    change.
    """
    offenders = refused_timeouts(suite_timeout_sites())

    assert not offenders, f"{len(offenders)} timeouts a loaded host cannot be granted any slack on:\n" + "\n".join(
        f"  {site.where}: {site.spelled}" for site in offenders
    )
