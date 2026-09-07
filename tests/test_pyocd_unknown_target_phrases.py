"""The pyOCD wording the classifier depends on, held against the installed pyOCD.

The classifier decides that a call failed on an unresolvable ``target_type`` by
matching pyOCD's console prose, and that match is the only thing that unlocks the
``pyocd pack find`` / ``pyocd pack install`` remediation. An earlier phrase list
matched nothing pyOCD actually prints, so the remediation was unreachable and
every such failure fell through to ``unknown_debugger_error`` with no mention of
CMSIS packs. The only proof that the current phrases fare better is a stand-in
that reproduces the sentence by hand, which cannot notice a reworded release.

So the sentence under test is produced by the installed pyOCD here, the way the
python-can facts behind the CAN classifier are asserted against the installed
library. A release that rewords the refusal fails these tests instead of quietly
returning ``unknown_debugger_error`` again.

No hardware and no network are involved, but not for the reason it would be
natural to assume. pyOCD's command line does **not** reject an unresolvable
target type before it goes looking for a probe: ``pyocd reset --target <unknown>``
prints "Waiting for a debug probe to be connected..." and waits, and with ``-W``
it fails on "No connected debug probes" without ever reaching the target type.
The type is resolved inside ``Board.__init__``, which runs while the session is
being built, after a probe has been opened. What makes a boardless test possible
is narrower and is asserted below: ``Board.__init__`` reaches its refusal reading
session options alone, so a probeless session (a documented pyOCD usage, "useful
to create a session that operates only as a container for session options") is
enough to make the real refusal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import write_config
from support import scaled_time_bound

from agentic_hil.backends.pyocd import TARGET_TYPE_INVALID_DOC, PyOCDBackend
from agentic_hil.config import load_config
from agentic_hil.tools import AgenticHILToolService

pytest.importorskip("pyocd", reason="pyOCD is an optional extra (install agentic-hil[pyocd])")

# Not a plausible near-miss of a real part: this must be unresolvable on any host,
# including one whose CMSIS-pack cache is full of vendor targets.
UNRESOLVABLE_TARGET_TYPE = "agentic_hil_no_such_target_type"


def real_pyocd_refusal(target_type: str = UNRESOLVABLE_TARGET_TYPE) -> str:
    """What the installed pyOCD says about a target type it cannot resolve.

    Built by driving pyOCD's own code rather than by quoting it. ``Session(None,
    ...)`` is pyOCD's documented options-container session, and ``Board`` is what
    raises; the probe stays ``None`` throughout, which is the evidence that this
    refusal is reachable with nothing attached.
    """
    from pyocd.board.board import Board
    from pyocd.core.exceptions import TargetSupportError
    from pyocd.core.session import Session

    session = Session(None, no_config=True, project_dir=".", target_override=target_type)
    assert session.probe is None
    with pytest.raises(TargetSupportError) as raised:
        Board(session)
    return str(raised.value)


# ---------------------------------------------------------------------------
# Library facts. Asserted against what is installed, because assuming them is
# what built the defect.


def test_the_installed_pyocd_refuses_an_unresolvable_target_type_without_a_probe() -> None:
    """The premise of every test below, pinned.

    If a release ever resolves this name, or reaches for the probe before
    refusing, the helper stops producing a refusal and says so here rather than
    somewhere further along where it would look like a classifier fault.
    """
    message = real_pyocd_refusal()

    assert UNRESOLVABLE_TARGET_TYPE in message


def test_the_markers_the_classifier_matches_on_are_still_in_the_real_message() -> None:
    """The two independent markers, named separately so a failure says which died.

    Either one alone classifies the message, so losing one is not yet the
    regression, but it is the drift that preceded it last time, and it is
    invisible unless something asserts it.
    """
    lower = real_pyocd_refusal().lower()

    assert "target type" in lower
    assert "not recognized" in lower
    assert TARGET_TYPE_INVALID_DOC in lower


# ---------------------------------------------------------------------------
# Classification.


def test_the_real_refusal_is_classified_as_a_target_type_problem(tmp_path: Path) -> None:
    """The guard the issue asks for, with nothing else able to rescue it.

    ``_classify_output`` is called directly rather than through a run, because a
    run would apply ``_confirm_target_support`` afterwards: the ``pyocd json
    --targets`` cross-check, which would reclassify a message the phrases missed
    and hide exactly the drift this test exists to catch.
    """
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd", target_type=UNRESOLVABLE_TARGET_TYPE)))
    backend = PyOCDBackend(config)

    assert backend._classify_output(real_pyocd_refusal(), "flash_firmware") == "target_type_invalid"


# ---------------------------------------------------------------------------
# Through the service: the remediation the phrases unlock.


STUB_PYOCD = '''#!/usr/bin/env python3
"""A pyOCD whose refusal is made by the installed pyOCD, not written out here.

Only the log envelope below belongs to the test. The sentence inside it is
whatever this pyOCD produces, so the backend classifies real wording carried over
the real transport: a subprocess, stderr, exit 1.
"""

import json
import sys

args = sys.argv[1:]

if "--version" in args:
    from pyocd import __version__

    print(__version__)
    raise SystemExit(0)

if args[:1] == ["json"]:
    if args[1:] == ["--probes", "--no-config"]:
        print(json.dumps({"status": 0, "boards": [{"unique_id": "PYOCD123"}]}))
        raise SystemExit(0)
    if args[1:] == ["--targets", "--no-config"]:
        # Deliberately unavailable. The backend cross-checks an unexplained
        # failure against this list and reclassifies on a definite absence, which
        # would classify the run correctly even if the phrases matched nothing.
        # A list that cannot be read never reclassifies, so what the assertions
        # below see is the phrase match and only the phrase match.
        print("`pyocd json --targets` is unavailable in this stub", file=sys.stderr)
        raise SystemExit(2)
    print("unsafe probe discovery arguments", file=sys.stderr)
    raise SystemExit(2)

from pyocd.board.board import Board
from pyocd.core.exceptions import TargetSupportError
from pyocd.core.session import Session

target = args[args.index("--target") + 1] if "--target" in args else "<unset>"
try:
    Board(Session(None, no_config=True, project_dir=".", target_override=target))
except TargetSupportError as error:
    print(f"0001042 C {error} [__main__]", file=sys.stderr)
    raise SystemExit(1) from None

print(f"pyOCD resolved {target}, which this test requires it to refuse", file=sys.stderr)
raise SystemExit(3)
'''


def stub_pyocd(tmp_path: Path) -> Path:
    path = tmp_path / "stub_pyocd.py"
    path.write_text(STUB_PYOCD, encoding="utf-8")
    return path


# The budget the stub runs under. It is a Python process that imports pyOCD
# twice, once for the probe list and once for the refusal, so five seconds is
# ample on an idle machine and runs out on a loaded one: the service then
# terminates the child, its stderr is empty, and every assertion below reads as
# a stub that stopped reaching pyOCD, which is the one failure this file exists
# to catch and is in that case the wrong answer.
#
# So the budget takes the same factor every wall-clock bound in the suite takes
# (#522), which keeps these tests measuring what they measured, and the run
# below reads the log envelope's `timed_out` before it reads any wording.
STUB_TIMEOUT_S = 5.0


def stub_config(directory: Path) -> Path:
    """The configuration both runs against the stub are driven from."""
    directory.mkdir(parents=True, exist_ok=True)
    return write_config(
        directory,
        debugger_type="pyocd",
        debugger_executable=stub_pyocd(directory),
        probe_id="PYOCD123",
        target_type=UNRESOLVABLE_TARGET_TYPE,
        timeout_s=scaled_time_bound(STUB_TIMEOUT_S),
    )


def run_the_stub(directory: Path) -> tuple[dict, dict]:
    """`probe_target` against the stub, as the refusal and the log it wrote.

    The envelope is read here, before either test reads a word of the refusal,
    because a terminated child makes every wording claim below false for a
    reason that has nothing to do with pyOCD. Reported as a failure and never as
    a skip: a file that stops asserting on exactly the hosts this is about would
    be green with the product broken.
    """
    config = load_config(str(stub_config(directory)))
    service = AgenticHILToolService(config)
    try:
        refused = service.call("probe_target")
    finally:
        service.close()
    log = json.loads((Path(config.workspace_root) / refused["log_path"]).read_text(encoding="utf-8"))
    assert log["timed_out"] is False, (
        f"the stub was terminated after {scaled_time_bound(STUB_TIMEOUT_S)}s instead of refusing, so nothing here is "
        f"about pyOCD's wording; this host needs a larger AGENTIC_HIL_TEST_TIME_SCALE"
    )
    return refused, log


def test_the_real_refusal_reaches_the_cmsis_pack_remediation(tmp_path: Path) -> None:
    """Stage 2: the words have to arrive as the fix, not only as a category.

    Driven through the real backend against a pyOCD that makes its own refusal,
    so the classifier, the catalogue lookup and the substituted command are
    exercised together on wording nobody in this repository chose.
    """
    refused, _ = run_the_stub(tmp_path)

    assert refused["ok"] is False
    assert refused["error_type"] == "target_type_invalid"
    assert refused["install_commands"] == [
        f"pyocd pack find {UNRESOLVABLE_TARGET_TYPE}",
        f"pyocd pack install {UNRESOLVABLE_TARGET_TYPE}",
        "pyocd pack show",
    ]
    assert any("pyocd pack install" in step for step in refused["remediation"])


def test_the_stub_carries_the_installed_pyocd_wording(tmp_path: Path) -> None:
    """The stub is only evidence if its refusal is the library's.

    Without this, a stub that silently stopped reaching pyOCD would keep the test
    above passing on wording of its own: the failure mode this whole file exists
    to remove.
    """
    _, log = run_the_stub(tmp_path)

    assert real_pyocd_refusal() in log["stderr"]
    assert log["returncode"] == 1
