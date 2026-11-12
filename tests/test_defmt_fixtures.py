"""Rust-free checks for the generated defmt test images."""

from __future__ import annotations

import conftest
import defmt_fixtures
import pytest


def test_fixture_skips_with_a_named_reason_when_cargo_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(defmt_fixtures.shutil, "which", lambda command: None if command == "cargo" else "rustc")

    with pytest.raises(pytest.skip.Exception, match="cargo not found on PATH"):
        conftest._require_defmt_toolchain()
