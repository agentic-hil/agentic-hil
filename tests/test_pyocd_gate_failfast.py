"""Contract for stopping pyOCD hardware recordings after the first failure."""

from __future__ import annotations

import shlex
from pathlib import Path

import yaml


def test_pyocd_recordings_stage_stops_before_starting_later_hardware_actions() -> None:
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "bench-gate.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["bench-tier"]["steps"]
    step = next(step for step in steps if "tests/bench/pyocd_recordings.py" in step.get("run", ""))
    command = shlex.split(step["run"])
    pytest_args = command[command.index("--") + 1 :]

    assert "-x" in pytest_args or "--exitfirst" in pytest_args or "--maxfail=1" in pytest_args, command
