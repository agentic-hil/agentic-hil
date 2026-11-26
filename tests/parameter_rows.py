"""Helpers for running folded parameter tables with per-row isolation."""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterable, Iterator
from itertools import product
from pathlib import Path

import pytest
from conftest import assert_still_sandboxed

_PYTEST_PARAMETER = type(pytest.param(None))
ParameterRow = tuple[str, tuple[object, ...]]


def parameter_rows(cases: Iterable, *, multiple: bool = False, ids: Iterable[str] | None = None) -> Iterator[ParameterRow]:
    """Carry the original parameter values and labels into an ordinary loop."""
    if ids is not None:
        labelled = zip(ids, cases, strict=True)
    else:
        labelled = ((getattr(case, "id", None) or str(case), case) for case in cases)
    for case_id, case in labelled:
        values = case.values if isinstance(case, _PYTEST_PARAMETER) else case if multiple else (case,)
        yield case_id, values


def combine_parameter_rows(*axes: Iterable[ParameterRow]) -> Iterator[ParameterRow]:
    """Match pytest's order for stacked decorators, innermost axis first."""
    for cases in product(*axes):
        yield "-".join(case_id for case_id, _ in cases), tuple(value for _, values in cases for value in values)


def check_parameter_rows(tmp_path: Path, rows: Iterable[ParameterRow], check_row: Callable[..., None]) -> None:
    """Run each row in its own workspace, with the suite's host protections in force."""
    problems = []
    sandbox = Path.home().parent
    for index, (case_id, values) in enumerate(rows):
        workspace = tmp_path / f"row-{index}"
        row_root = sandbox / f"row-{index}"
        home = row_root / "home"
        config = row_root / "config"
        state = row_root / "state"
        temp = row_root / "tmp"
        workspace.mkdir()
        home.mkdir(parents=True)
        temp.mkdir()
        # Keep the row's patch separate so undoing it cannot restore the real profile.
        with pytest.MonkeyPatch.context() as isolation, pytest.MonkeyPatch.context() as patch:
            isolation.setenv("HOME", str(home))
            isolation.setenv("USERPROFILE", str(home))
            isolation.setenv("APPDATA", str(config))
            isolation.setenv("XDG_CONFIG_HOME", str(config))
            isolation.setenv("XDG_CACHE_HOME", str(home / ".cache"))
            isolation.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
            isolation.setenv("LOCALAPPDATA", str(state))
            isolation.setenv("XDG_STATE_HOME", str(state))
            for variable in ("TMPDIR", "TEMP", "TMP"):
                isolation.setenv(variable, str(temp))
            isolation.setattr(tempfile, "tempdir", str(temp))
            try:
                check_row(workspace, patch, *values)
            except (Exception, pytest.fail.Exception) as error:
                problems.append(f"[{case_id}] {type(error).__name__}: {error}")
            finally:
                assert_still_sandboxed(f"after parameter row {case_id} ran")

    assert not problems, "\n".join(problems)
