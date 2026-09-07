"""The suite's one time scale factor, reached from inside the image (#522).

The container tier holds wall-clock ceilings of its own, and they are the ones a
loaded host breaks first: a call against a hanging OpenOCD and a probeless pyOCD
refusal both measure a real program starting under a real scheduler. So they
take the same factor the rest of the suite takes, which means the tier has to be
able to import `tests/support.py`.

Nothing has to be arranged for that and this file is the proof rather than the
arrangement. The image copies this checkout in whole, the tier is a package
under `tests/`, and pytest puts the first directory above the package on the
path, which is the directory the helper lives in. A source distribution carries
it for the same reason: `recursive-include tests *.py`, with no exclusion
naming it.

What this cannot be replaced by is the same assertion in the plain tier. The
question is whether the import resolves where the container tests run, and the
only place that can be answered is where they run.
"""

from __future__ import annotations

import pytest
from support import TIME_SCALE_MAXIMUM, TIME_SCALE_MINIMUM, TIME_SCALE_VARIABLE, scaled_time_bound

from .conftest import CONTAINER_ONLY

pytestmark = [pytest.mark.container, CONTAINER_ONLY]

# The tier's own ceilings, so the numbers this exercises are the numbers the
# tier holds rather than a round example: the hanging-debugger call ceiling in
# test_debugger_processes_against_openocd.py and the probeless pyOCD bound in
# test_pyocd_without_a_probe.py, which is half of a configured 40 s timeout.
CALL_CEILING_S = 15.0
PROBELESS_PYOCD_BOUND_S = 20.0


def test_the_helper_beside_the_suite_is_importable_from_the_container_tier() -> None:
    """The import at the top of this file is the assertion; this names it.

    A collection error would be the real failure and would name this file, but a
    reader looking for the claim should find it written down as one.
    """
    assert callable(scaled_time_bound)
    assert TIME_SCALE_VARIABLE == "AGENTIC_HIL_TEST_TIME_SCALE"
    assert (TIME_SCALE_MINIMUM, TIME_SCALE_MAXIMUM) == (1.0, 100.0)


def test_the_tier_s_own_ceilings_are_unchanged_when_no_factor_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default inside the image is the image the tier already had."""
    monkeypatch.delenv(TIME_SCALE_VARIABLE, raising=False)

    assert scaled_time_bound(CALL_CEILING_S) == CALL_CEILING_S
    assert scaled_time_bound(PROBELESS_PYOCD_BOUND_S) == PROBELESS_PYOCD_BOUND_S


def test_a_factor_widens_the_tier_s_own_ceilings(monkeypatch: pytest.MonkeyPatch) -> None:
    """And a job that knows its runner is loaded sets one variable for both."""
    monkeypatch.setenv(TIME_SCALE_VARIABLE, "3")

    assert scaled_time_bound(CALL_CEILING_S) == 45.0
    assert scaled_time_bound(PROBELESS_PYOCD_BOUND_S) == 60.0
