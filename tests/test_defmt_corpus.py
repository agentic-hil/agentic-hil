"""Immutable replay inputs, verified without the decoder or Git at test time."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_BLOB_OIDS = {'tests/fixtures/defmt/expected-errors.jsonl': 'e253f211747aa175318829bc0d5526d2e9cf99a8', 'tests/fixtures/defmt/expected-raw.jsonl': 'ecf091bbd5c5c1e9c075edffe701a489db8ca3e2', 'tests/fixtures/defmt/expected-rzcobs.jsonl': '5903ba28c498f2e834c43b6ee13f59cea7453989', 'tests/fixtures/defmt/firmware/.cargo/config.toml': '470e0876e64eeaf1f0d6ce6b90915906b0ab7375', 'tests/fixtures/defmt/firmware/Cargo.lock': '130e9b6b375da07006d2173e14fd5d3d9807bf68', 'tests/fixtures/defmt/firmware/Cargo.toml': '715cc7ff692b881dba86b967e2f653be514cef01', 'tests/fixtures/defmt/firmware/build.rs': '03cd5abdc4a8516eaec567a7161a0cd9ae167a26', 'tests/fixtures/defmt/firmware/memory.x': '7695220d7f9fdf26dbd51eb50feb349c94f0ddbc', 'tests/fixtures/defmt/firmware/src/main.rs': 'be0f20a8c12e233f8100ca87527f3ceb8c3f0e76', 'tests/fixtures/defmt/reference-decoder/Cargo.lock': 'e7b2dca84c468cd82a7c0e77d16de9d6f515f661', 'tests/fixtures/defmt/reference-decoder/Cargo.toml': '3eada823005b8b3f53964a72ae32f73cfa65db61', 'tests/fixtures/defmt/reference-decoder/src/main.rs': 'a931f325f0e4074667a1241945b0c81dd3b3cd47', 'tests/fixtures/defmt/scenario.py': '27b00d789dd8a35ff9bafb4055dbe4161afe7788', 'tests/fixtures/defmt/stream-errors.bin': 'ea08fd37dee19985ae45653dea931d0a3b73e54d', 'tests/fixtures/defmt/stream-raw.bin': 'a8ac6c051223777364e74be261e1c006f594e585', 'tests/fixtures/defmt/stream-rzcobs.bin': '5edb01a05b8ca5a5feb5988aa3e16f6d4c53bdfb'}


def test_fixture_bytes_match_committed_source_blobs():
    assert len(FIXTURE_BLOB_OIDS) == 16
    for name, expected in FIXTURE_BLOB_OIDS.items():
        data = (ROOT / name).read_bytes()
        header = b"blob " + str(len(data)).encode("ascii") + b"\0"
        assert hashlib.sha1(header + data).hexdigest() == expected, name


def test_oracles_have_the_preserved_record_counts_and_error_shape():
    fixtures = ROOT / "tests/fixtures/defmt"
    for name, count in (("raw", 46), ("rzcobs", 46), ("errors", 9)):
        records = [json.loads(line) for line in (fixtures / f"expected-{name}.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(records) == count
        assert (fixtures / f"stream-{name}.bin").read_bytes()
        if name == "errors":
            assert [record.get("error") for record in records] == ["malformed", None, "malformed", "malformed", "malformed", "malformed", "malformed", None, None]
        else:
            assert all("error" not in record for record in records)
        for record in records:
            if "error" not in record:
                assert isinstance(record["message"], str) and record["message"]
                assert isinstance(record["line"], str) and record["line"].endswith(record["message"])
