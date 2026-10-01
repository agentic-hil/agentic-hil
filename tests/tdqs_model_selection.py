"""Leave the real-model test out of every run unless it is asked for.

Tests marked `tdqs_model` call the model the tool-definition score uses, which
costs model calls and needs a login. They are deselected unless
AGENTIC_HIL_TDQS_MODEL is exactly 1, which the score job sets for the one step
that runs them.
"""

from __future__ import annotations

import os

import pytest

MARKER = "tdqs_model"
SWITCH = "AGENTIC_HIL_TDQS_MODEL"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(SWITCH) == "1":
        return
    deselected = [item for item in items if item.get_closest_marker(MARKER) is not None]
    if not deselected:
        return
    config.hook.pytest_deselected(items=deselected)
    items[:] = [item for item in items if item.get_closest_marker(MARKER) is None]
