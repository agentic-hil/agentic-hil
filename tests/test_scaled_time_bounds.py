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
from collections.abc import Callable
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
# `test_run_lifecycle.py` holds the only one. `step["waited_ms"] < 600_000`
# reads a number the product wrote into its own step envelope and holds it
# against the ten minutes the plan asked for, so the claim is that a stop cut
# the wait short. The product answers that the same way on a fast host and a
# slow one; a factor there would loosen a product claim while granting a loaded
# host nothing, which is the opposite of what this issue is for.
EXEMPT_BOUNDS = {"test_run_lifecycle.py": ('step["waited_ms"] < 600_000',)}


def is_exempt(entry: Comparison) -> bool:
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
    offenders = [entry for entry in suite_ceilings() if not entry.through_the_helper]

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


def test_nothing_outside_a_helper_call_is_part_of_a_bound() -> None:
    """What may stand after the call is a unit conversion, and nothing else.

    `scaled_time_bound(CONFIGURED_TIMEOUT_S) * 1000` is the same bound in
    milliseconds. `scaled_time_bound(A) + B` is half a bound, and it is the
    shape a sweep produces when it wraps the first name it meets.
    """
    conversion = re.compile(r"^(?:[*/]\s*[0-9][0-9_.]*)?$")
    outside = [entry for entry in suite_ceilings() if not conversion.match(entry.outside_the_helper)]

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
# constant, so its default is what is pinned.
BASE_CONSTANTS = {
    "bench/test_bench_coordination.py": ("REFUSAL_CEILING_S = 30.0", "ASKED_WAIT_S = 3.0"),
    "container/test_can_over_vcan.py": ("BUS_TIMEOUT_S = 2.0", "WAIT_SLACK_S = 1.5"),
    "container/test_debugger_processes_against_openocd.py": ("CALL_CEILING_S = 15.0",),
    "container/test_pyocd_without_a_probe.py": ("TIMEOUT_S = 40",),
    "test_bench_mutex.py": ("FRESH_HEARTBEAT_AGE_S = 1.0",),
    "test_com_stdio_bridge.py": ("SHUTDOWN_CEILING_S = 1.5",),
    "test_debug_backend_refusals.py": ("START_TIMEOUT_S = 10.0",),
    "test_debugger_processes.py": ("CALL_CEILING_S = 15.0", "EMPTIED_GROUP_CEILING_S = 4.0"),
    "test_pyocd_without_a_probe.py": ("CONFIGURED_TIMEOUT_S = 5",),
    "test_run_lifecycle.py": ("timeout_s: float = 60.0",),
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
