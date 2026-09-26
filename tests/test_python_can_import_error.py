"""A python-can that will not import says what the import raised (#569).

`can_session_start` answered a failed `import can` with
`can_backend_not_available` and the sentence "python-can is not installed.
Install agentic-hil[can] to use direct CAN adapters.", and it dropped the error
itself. A python-can that is installed and fails inside its own imports read as
a missing package, and the remedy sent the operator to install a package that
was already there. The result now carries the import error's own line in
`backend_error`, with its type name, the way `can_adapter_open_failed` carries
the backend's line; its summary no longer says the package is missing when the
import failed for another reason; and every screen that prints the failure
shows the line.

Each failure is produced for real, through the import system: python-can hidden
from every finder, `can` blocked in `sys.modules`, and a stand-in `can` package
whose own code imports a module no host has. Every change to `sys.modules`,
`sys.meta_path` and `sys.path` goes back through `monkeypatch`, so the next test
imports the real python-can.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import write_authoritative_config, write_config

from agentic_hil import cli
from agentic_hil.config import load_config
from agentic_hil.tools import AgenticHILToolService
from agentic_hil.types import JsonObject

# The summary the decided behaviour gives, which no longer says the package is
# missing when the import failed for another reason.
SUMMARY = "python-can is not installed or could not be imported. Install agentic-hil[can] to use direct CAN adapters."
# The causes the refusal keeps giving, unchanged by the fix: the third is the
# broken installation, so the catalogue already says what this issue adds.
LIKELY_CAUSES = [
    "python-can is not installed for the interpreter running this server, and `agentic-hil[can]` is what installs it",
    "this server runs on a different interpreter than the one the backend was installed into",
    "the installation is broken rather than absent, and importing the backend raises where the driver bindings are loaded",
]

# The import system's own line for a package that is not there.
MISSING = "ModuleNotFoundError: No module named 'can'"
# The module the stand-in python-can's own code imports, the way the real one
# imports its dependencies, and the line the import system raises for it.
INNER_MODULE = "can_driver_bindings"
BROKEN_INSIDE = f"ModuleNotFoundError: No module named '{INNER_MODULE}'"

# The three ways the issue says read the same.
KINDS = ("missing", "blocked", "broken_inside")

# Distinctive by design: device locks are machine-wide, so a bus id and channel
# shared with another clone's tests would contend across checkouts. The
# adapter goes through python-can and has no channel rule in front of the
# import, so the import is the first thing the open meets on every host.
BUS_ID = "python_can_import_bus"
CHANNEL = "can569import"
BUS_YAML = f'can_buses:\n  {BUS_ID}:\n    adapter: "socketcan"\n    channel: "{CHANNEL}"\n'

PLAN = f"version: 2\nsteps:\n  - {{bus_id: {BUS_ID}, action: can_open}}\n"


# ---------------------------------------------------------------------------
# Breaking python-can, for real.


def _forget_can(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take python-can out of `sys.modules`, so the next import of it is a real one.

    Every name is registered with `monkeypatch` before it is removed: whatever a
    test leaves under these names is taken out again at the end, and the real
    modules, where there were any, are put back."""
    names = {"can"} | {name for name in sys.modules if name == "can" or name.startswith("can.")}
    for name in sorted(names):
        # `setitem` records whether the name was there and what it held; the
        # `delitem` after it leaves the name absent for the test.
        monkeypatch.setitem(sys.modules, name, None)
        monkeypatch.delitem(sys.modules, name)


class _WithoutPythonCan:
    """The host's own finders, asked for everything except python-can.

    Taking python-can's directory off `sys.path` would hide every module
    installed beside it too, and a finder that only declined `can` in front of
    the others would be passed over for the next one, which finds it. This
    stands in for all of them: `can` is found nowhere and the import system
    raises its own error for it, while every other module, and every
    distribution `importlib.metadata` looks up, is answered by the finder that
    always did."""

    def __init__(self, finders: list[object]) -> None:
        self._finders = finders

    def find_spec(self, name: str, path: object = None, target: object = None) -> object:
        if name == "can" or name.startswith("can."):
            return None
        for finder in self._finders:
            find_spec = getattr(finder, "find_spec", None)
            spec = find_spec(name, path, target) if find_spec is not None else None
            if spec is not None:
                return spec
        return None

    def find_distributions(self, *args: object, **kwargs: object) -> Iterator[object]:
        for finder in self._finders:
            find_distributions = getattr(finder, "find_distributions", None)
            if find_distributions is not None:
                yield from find_distributions(*args, **kwargs)

    def invalidate_caches(self) -> None:
        for finder in self._finders:
            invalidate_caches = getattr(finder, "invalidate_caches", None)
            if invalidate_caches is not None:
                invalidate_caches()


def _stand_in_python_can(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `can` package, first on `sys.path`, whose `__init__.py` imports
    `INNER_MODULE`, the way the real one imports its dependencies there."""
    root = tmp_path / "stand-in-python-can"
    package = root / "can"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'"""A stand-in python-can."""\nimport {INNER_MODULE}\n', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))


def _raised_importing_can() -> str:
    """The line the import system raises for `import can`, on the host as the
    test has left it."""
    try:
        __import__("can")
    except ImportError as error:
        return f"{type(error).__name__}: {error}"
    raise AssertionError("can imported on a host that was set up so it could not")


def _break_python_can(kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Leave python-can unimportable the way `kind` names, and return the line
    the import system raises for `import can`, which is what `backend_error` has
    to say.

    `missing` and `broken_inside` raise lines the decided behaviour names, so
    the host is checked for them before the product is asked anything; `blocked`
    raises whatever the import system raises, and that is the expectation."""
    _forget_can(monkeypatch)
    if kind == "missing":
        monkeypatch.setattr(sys, "meta_path", [_WithoutPythonCan(list(sys.meta_path))])
    elif kind == "blocked":
        monkeypatch.setitem(sys.modules, "can", None)
    else:
        _stand_in_python_can(tmp_path, monkeypatch)
    raised = _raised_importing_can()
    if kind == "missing":
        assert raised == MISSING, raised
    elif kind == "broken_inside":
        assert raised == BROKEN_INSIDE, raised
    return raised


# ---------------------------------------------------------------------------
# The configuration and the calls the results are read on.


def _can_config(workspace: Path):
    return load_config(str(write_config(workspace, can_buses_yaml=BUS_YAML)))


def _session_start(config) -> JsonObject:
    service = AgenticHILToolService(config)
    try:
        return service.call("can_session_start", {"bus_id": BUS_ID})
    finally:
        service.close()


def _flat(text: str) -> str:
    """The text with the wrapper's line breaks taken back out."""
    return " ".join(text.split())


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = cli.entrypoint(list(argv))
    return code, capsys.readouterr().out


# ---------------------------------------------------------------------------
# The result.


@pytest.mark.parametrize("kind", KINDS)
def test_can_session_start_carries_the_import_error(kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`can_session_start` keeps its error type and causes, says the package is
    missing or could not be imported, and carries the import error's own line,
    with its type name. Nothing was opened."""
    config = _can_config(tmp_path / "workspace")
    raised = _break_python_can(kind, tmp_path, monkeypatch)

    result = _session_start(config)

    assert result["ok"] is False, result
    assert result["error_type"] == "can_backend_not_available", result
    assert result["summary"] == SUMMARY, result
    assert result["likely_causes"] == LIKELY_CAUSES, result
    assert result["side_effect_committed"] is False, result
    assert result.get("backend_error") == raised, result


def test_the_three_failures_read_differently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The issue's complaint, whole: python-can missing, blocked, and failing
    inside its own imports read the same. They now read as three different
    lines, and the two the decided behaviour names are those lines exactly:
    `No module named 'can'`, and the inner module's name."""
    config = _can_config(tmp_path / "workspace")
    answers: dict[str, object] = {}
    for kind in KINDS:
        with monkeypatch.context() as patch:
            _break_python_can(kind, tmp_path / kind, patch)
            answers[kind] = _session_start(config).get("backend_error")

    assert answers["missing"] == MISSING, answers
    assert answers["broken_inside"] == BROKEN_INSIDE, answers
    assert len(set(answers.values())) == 3, answers


# ---------------------------------------------------------------------------
# The screens that print the failure.


def test_the_report_the_refusal_leaves_carries_the_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`get_last_report` answers with the refusal as it was recorded."""
    config = _can_config(tmp_path / "workspace")
    raised = _break_python_can("broken_inside", tmp_path, monkeypatch)

    service = AgenticHILToolService(config)
    try:
        refused = service.call("can_session_start", {"bus_id": BUS_ID})
        recorded = service.call("get_last_report", {})["report"]
    finally:
        service.close()

    assert refused["error_type"] == "can_backend_not_available", refused
    assert recorded["error_type"] == "can_backend_not_available", recorded
    assert recorded.get("backend_error") == raised, recorded


def test_a_plan_that_opens_the_bus_prints_the_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`agentic-hil test-reactor` on a plan whose `can_open` step meets the
    failure: the step's result carries the line in the document, and the screen
    shows it."""
    workspace = tmp_path / "workspace"
    write_authoritative_config(workspace, monkeypatch, can_buses_yaml=BUS_YAML)
    (workspace / "can-open.testconfig.yaml").write_text(PLAN, encoding="utf-8")
    monkeypatch.chdir(workspace)
    raised = _break_python_can("broken_inside", tmp_path, monkeypatch)

    code, out = _cli(capsys, "test-reactor", "--test-config", "can-open.testconfig.yaml", "--json")
    assert code == 1, out
    document = json.loads(out)
    step = document["steps"][0]
    assert step["action"] == "can_open", step
    assert step["result"]["error_type"] == "can_backend_not_available", step
    assert step["result"].get("backend_error") == raised, step

    code, out = _cli(capsys, "test-reactor", "--test-config", "can-open.testconfig.yaml")
    assert code == 1, out
    assert raised in _flat(out), out
