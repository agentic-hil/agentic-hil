from __future__ import annotations

import sys
from importlib.metadata import version
from pathlib import Path

from packaging.requirements import Requirement

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def test_dev_extra_preserves_process_table_grouping() -> None:
    document = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    requirements = [
        Requirement(raw) for raw in document["project"]["optional-dependencies"]["dev"]
    ]
    pytest_requirements = [requirement for requirement in requirements if requirement.name.lower() == "pytest"]
    assert len(pytest_requirements) == 1
    specifier = pytest_requirements[0].specifier
    assert "9.1.1" in specifier, "dev extra must accept the verified pytest 9.1.1"
    if "9.2.0" in specifier:
        raise AssertionError("dev extra permits pytest 9.2.0, which breaks process-table grouping")
    assert version("pytest") in specifier, "installed pytest must satisfy the declared dev requirement"
