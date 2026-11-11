"""Fresh official and Python defmt output must agree with the preserved oracle."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import defmt_reference  # noqa: E402, I001


SAMPLE_RECORDS = [
    {"index": 7, "level": "INFO", "timestamp": None, "message": "first", "line": "INFO first"},
    {"index": 8, "level": "INFO", "timestamp": None, "message": "second", "line": "INFO second"},
]
SOURCES = ("expected", "official_committed", "python_committed", "official_rebuilt")


def sources_with_sample_records() -> dict[str, list[dict]]:
    return {name: deepcopy(SAMPLE_RECORDS) for name in SOURCES}


def test_compare_records_accepts_agreement():
    sources = sources_with_sample_records()

    discrepancies = defmt_reference.compare_records("rzcobs", sources)

    assert discrepancies == []


def test_compare_records_reports_disagreements():
    sources = sources_with_sample_records()
    sources["official_committed"][0]["message"] = "changed"
    sources["python_committed"].pop()

    discrepancies = defmt_reference.compare_records("rzcobs", sources)

    assert discrepancies == [
        {
            "stream": "rzcobs",
            "record": 1,
            "field": "message",
            "sources": ["expected", "official_committed"],
            "expected": "first",
            "actual": "changed",
        },
        {
            "stream": "rzcobs",
            "record": 2,
            "field": "$record",
            "sources": ["expected", "python_committed"],
            "expected": "present",
            "actual": "missing",
        },
    ]
