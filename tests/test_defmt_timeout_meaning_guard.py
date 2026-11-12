"""Guard the timeout meaning claim in the run coordination catalogue tests."""

from __future__ import annotations

from test_error_catalogue_ec3_run_coordination import SAYS, catalogue_entry, spec_violations

KEY = "defmt_expect_timeout"


def test_defmt_timeout_claim_shape_and_acceptance() -> None:
    if SAYS[KEY].meaning[1] != ("timeout_s|timeout",):
        raise AssertionError("defmt timeout claim must be the singleton timeout_s|timeout tuple")

    entry = catalogue_entry(KEY)
    assert entry is not None
    entry = {
        **entry,
        "meaning": "The defmt_expect step awaited a decoded frame until timeout while reads counted input_status.",
    }
    assert spec_violations(KEY, entry) == []


def test_defmt_timeout_claim_rejects_missing_timeout() -> None:
    entry = catalogue_entry(KEY)
    assert entry is not None
    entry = {
        **entry,
        "meaning": "The defmt_expect step awaited a decoded frame while reads counted input_status.",
    }
    problems = spec_violations(KEY, entry)
    assert len(problems) == 1
    assert problems[0].startswith("the meaning does not say")
    assert "timeout" in problems[0]
