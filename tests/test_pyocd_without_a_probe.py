"""What the three pyOCD tools answer when no probe is attached (#480).

An unplugged probe is the commonest bench fault and the one a result has to
name. On `type: pyocd` it was reported as a timeout instead: pyOCD waits for a
probe to appear unless it is spawned with `-W`, the backend never passed the
flag, so `probe_target`, `reset_target` and `flash_firmware` sat out
`timeout_s`, were reaped, declared their hardware state unknown and opened a
quarantine for a board nothing had contacted. The same fault on the ST-Link
backend answers `adapter_not_found` with `target_contacted: false` and no
cleanup requirement.

Passing the flag is half of it. With `-W` pyOCD 0.45.1 prints `No connected
debug probes`, or `No connected debug probe matches unique ID '<id>'` when a
`--uid` was given, and neither sentence was in the classifier's list, so the
result became `unknown_debugger_error` and still quarantined. The fixture these
tests drive answers with pyOCD's recorded words and, like pyOCD, only exits on
its own when it was told not to wait; the recording it reproduces is quoted in
`tests/fixtures/fake_pyocd_no_probe.py` with the version and the date.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import FAKE_GDB, elf_with_symbols, write_config
from support import scaled_time_bound

from agentic_hil.backends.pyocd import PyOCDBackend
from agentic_hil.config import load_config
from agentic_hil.tools import AgenticHILToolService

FAKE_PYOCD_NO_PROBE = Path(__file__).resolve().parent / "fixtures" / "fake_pyocd_no_probe.py"

# pyOCD 0.45.1 with no probe attached, recorded on 2026-09-06 in the container
# test image. The fixture reproduces these byte for byte and a test below holds
# it to that.
RECORDED_NO_PROBE = "No connected debug probes\n"
RECORDED_NO_PROBE_FOR_UID = "No connected debug probe matches unique ID 'NOSUCHPROBE0001'\n"
RECORDED_RESET_STDERR = "0000212 E No target device available to reset [reset_cmd]\n"
RECORDED_FLASH_STDERR = "0000223 E No target device available [load_cmd]\n"
RECORDED_WAITING = "Waiting for a debug probe to be connected...\n"

# `write_config` writes `timeout_s: 5`; a refusal that took that long is the
# wait the issue measured, not a refusal.
#
# Every comparison against this number goes through `scaled_time_bound`, which
# multiplies it by `AGENTIC_HIL_TEST_TIME_SCALE` and leaves it alone when that
# variable is unset (#515). The base stays what the claim needs; only the
# allowance a loaded host gets on top of it is configurable, because a run that
# measured 5.07 s here had nothing wrong with it but the machine it shared.
CONFIGURED_TIMEOUT_S = 5

THE_THREE_TOOLS = [
    ("probe_target", {}),
    ("reset_target", {"mode": "run"}),
    ("flash_firmware", {"image_path": "build/firmware.elf"}),
]


def config_for(workspace: Path, **kwargs):
    firmware = workspace / "build" / "firmware.elf"
    firmware.parent.mkdir(parents=True, exist_ok=True)
    firmware.write_bytes(b"\x7fELFfake")
    return load_config(str(write_config(workspace, debugger_type="pyocd", debugger_executable=FAKE_PYOCD_NO_PROBE, **kwargs)))


def blocking_record_states(config) -> set[str]:
    records = Path(config.state_root) / "coordination" / "records"
    if not records.is_dir():
        return set()
    states = set()
    for path in records.glob("*.json"):
        state = json.loads(path.read_text(encoding="utf-8")).get("state")
        if isinstance(state, str):
            states.add(state)
    return states & {"cleanup_required", "quarantined", "recovery_pending"}


def log_of(config, result: dict) -> dict:
    return json.loads((Path(config.workspace_root) / result["log_path"]).read_text(encoding="utf-8"))


def written_logs(config) -> list[Path]:
    return sorted((Path(config.workspace_root) / ".agentic-hil" / "logs").glob("pyocd-*.log"))


def assert_refused_before_contact(result: dict, config) -> None:
    """The issue's expected result, field by field, and no quarantine anywhere."""
    assert result["ok"] is False, result
    assert result["error_type"] == "adapter_not_found", result
    assert result["target_contacted"] is False, result
    assert result["side_effect_status"] == "not_started", result
    assert result["hardware_state"] == "unchanged", result
    assert result.get("cleanup_required") is not True, result
    assert result.get("quarantine_id") is None, result
    assert result.get("quarantined") is not True, result
    assert not blocking_record_states(config), blocking_record_states(config)


def run_fixture(*arguments: str, timeout: float = 30.0, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(FAKE_PYOCD_NO_PROBE), *arguments], capture_output=True, text=True, timeout=timeout, env=env, check=False)


# ---------------------------------------------------------------------------
# The fixture is only evidence if it says what the recorded pyOCD said.


def test_the_fixture_answers_the_way_the_recorded_pyocd_did(tmp_path: Path) -> None:
    commander = run_fixture("commander", "-W", "--command", "status")
    assert (commander.stdout, commander.stderr, commander.returncode) == (RECORDED_NO_PROBE, "", 0)

    by_uid = run_fixture("commander", "-W", "--uid", "NOSUCHPROBE0001", "--command", "status")
    assert (by_uid.stdout, by_uid.stderr, by_uid.returncode) == (RECORDED_NO_PROBE_FOR_UID, "", 0)

    reset = run_fixture("reset", "-W")
    assert (reset.stdout, reset.stderr, reset.returncode) == (RECORDED_NO_PROBE, RECORDED_RESET_STDERR, 1)

    flash = run_fixture("flash", "-W", "--no-reset", "--base-address", "0x08000000", str(tmp_path / "x.bin"))
    assert (flash.stdout, flash.stderr, flash.returncode) == (RECORDED_NO_PROBE, RECORDED_FLASH_STDERR, 1)

    listing = run_fixture("json", "--probes", "--no-config")
    assert listing.returncode == 0
    assert json.loads(listing.stdout) == {"pyocd_version": "0.45.1", "version": {"major": 1, "minor": 1}, "status": 0, "boards": []}


def test_the_fixture_waits_for_a_probe_when_it_is_not_told_not_to() -> None:
    """The half of the recording that made the fault a timeout: no `-W`, no exit."""
    with pytest.raises(subprocess.TimeoutExpired) as waited:
        run_fixture("commander", "--command", "status", timeout=3.0)

    # What the child had written when it was killed, in whichever form this
    # platform's `run` hands it back.
    partial = waited.value.stdout
    said = partial.decode() if isinstance(partial, bytes) else (partial or "")
    assert said == RECORDED_WAITING, said


# ---------------------------------------------------------------------------
# The classifier, on pyOCD's own sentences.


@pytest.mark.parametrize(
    ("output", "tool"),
    [
        (RECORDED_NO_PROBE, "probe_target"),
        (RECORDED_NO_PROBE + RECORDED_RESET_STDERR, "reset_target"),
        (RECORDED_NO_PROBE + RECORDED_FLASH_STDERR, "flash_firmware"),
        (RECORDED_NO_PROBE_FOR_UID, "probe_target"),
        (RECORDED_NO_PROBE_FOR_UID + RECORDED_RESET_STDERR, "reset_target"),
        (RECORDED_NO_PROBE_FOR_UID + RECORDED_FLASH_STDERR, "flash_firmware"),
    ],
)
def test_pyocds_own_no_probe_sentences_classify_as_a_missing_probe(tmp_path: Path, output: str, tool: str) -> None:
    """Both sentences, under each of the three tools, ahead of every other rule.

    The reset and flash outputs carry a second line about the missing target,
    which the reset and flash buckets would otherwise claim; the sentence about
    the probe is the one that says where the run stopped. The `[reset_cmd]`
    line is what `pyocd reset` prints; the backend's `reset_target` runs the
    commander instead, which prints the sentence alone and exits 0, and the
    service-level test below carries that real shape. Here the pairing is the
    harder one for the classifier, a reset-worded line to be outranked.
    """
    backend = PyOCDBackend(config_for(tmp_path))

    assert backend._classify_output(output, tool) == "probe_not_found"


def test_the_older_no_probe_phrases_still_classify_the_same_way(tmp_path: Path) -> None:
    """The neighbours in the same list stay: adding two sentences drops none."""
    backend = PyOCDBackend(config_for(tmp_path))

    for older in ("No available debug probes", "Unable to open probe", "No probe with UID 123"):
        assert backend._classify_output(older, "probe_target") == "probe_not_found", older


# ---------------------------------------------------------------------------
# Through the service: the spawn, the result and the absence of a quarantine.


@pytest.mark.parametrize(("tool", "arguments"), THE_THREE_TOOLS)
def test_a_tool_with_no_probe_attached_refuses_promptly_instead_of_timing_out(tmp_path: Path, tool: str, arguments: dict) -> None:
    """No `probe_id`, which is the field's default and what a one-probe bench is documented to use.

    Three things at once, because the issue measured all three going wrong
    together: the spawn tells pyOCD not to wait, the result is the refusal the
    ST-Link backend already gives for the same fault, and the call ends well
    inside `timeout_s` with one pyOCD run behind it rather than a reaped child
    and two recovery resets that each waited the same time again.
    """
    config = config_for(tmp_path)
    service = AgenticHILToolService(config)
    try:
        started = time.perf_counter()
        result = service.call(tool, arguments)
        elapsed_s = time.perf_counter() - started
    finally:
        service.close()

    assert result["error_type"] != "timeout", result
    assert_refused_before_contact(result, config)
    # The product's own clock around the pyOCD run is the claim: a call that
    # waited out the timeout measures at least the timeout there. The wall
    # clock around the whole call also holds two interpreter starts and
    # whatever the host was doing beside them, so it is held to twice the
    # timeout, which the three waits the issue measured still cannot pass.
    assert result["elapsed_ms"] < scaled_time_bound(CONFIGURED_TIMEOUT_S) * 1000, result
    assert elapsed_s < scaled_time_bound(2 * CONFIGURED_TIMEOUT_S), (elapsed_s, result)
    # pyOCD's own sentence travels with the refusal, as the evidence it is.
    assert "No connected debug probes" in json.dumps(result), result
    log = log_of(config, result)
    assert log["timed_out"] is False, log
    assert "-W" in log["command"].split() or "--no-wait" in log["command"].split(), log["command"]
    assert log["stdout"] == RECORDED_NO_PROBE, log
    # One run, and no recovery reset after it: a call that never contacted the
    # board has nothing to recover.
    assert len(written_logs(config)) == 1, [path.name for path in written_logs(config)]


def test_a_probe_that_vanished_after_its_uid_was_resolved_is_refused_the_same_way(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other route to the wait: a configured `probe_id` whose enumeration answered.

    The fast refusal that existed came from the enumeration, and once a UID has
    been resolved the enumeration is skipped, so a probe unplugged after the
    first successful call reached the same wait. Here the listing still names
    the probe, the connect cannot find it, and the second call takes the cached
    route: both have to answer with pyOCD's `--uid` sentence, at once.
    """
    monkeypatch.setenv("AGENTIC_HIL_FAKE_PYOCD_PROBES", json.dumps(["PYOCD123"]))
    config = config_for(tmp_path, probe_id="PYOCD123")
    service = AgenticHILToolService(config)
    try:
        started = time.perf_counter()
        first = service.call("probe_target")
        second = service.call("reset_target", {"mode": "run"})
        elapsed_s = time.perf_counter() - started
    finally:
        service.close()

    for result in (first, second):
        assert result["error_type"] != "timeout", result
        assert_refused_before_contact(result, config)
        log = log_of(config, result)
        assert "--uid PYOCD123" in log["command"], log["command"]
        assert log["stdout"] == "No connected debug probe matches unique ID 'PYOCD123'\n", log
        # The claim is that neither call waited out the configured timeout, so
        # each is judged by its own clock, the one the product measured around
        # the pyOCD run. The wall clock around both spans two process starts
        # and whatever the host was doing beside them, so it is held to twice
        # the timeout: a call that did wait it out cannot hide under that bound
        # beside one that did not.
        assert result["elapsed_ms"] < scaled_time_bound(CONFIGURED_TIMEOUT_S) * 1000, result
    assert elapsed_s < scaled_time_bound(2 * CONFIGURED_TIMEOUT_S), elapsed_s
    assert len(written_logs(config)) == 2, [path.name for path in written_logs(config)]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("debug_symbol_value", {"symbol": "boot_counter"}),
        ("debug_dump_symbol_ihex", {"symbol": "boot_counter", "output_path": "build/boot_counter.hex"}),
    ],
)
def test_a_sessionless_read_with_no_probe_attached_is_refused_the_same_way(tmp_path: Path, tool: str, arguments: dict) -> None:
    """The two reads the issue does not name, which share the same connection half.

    `debug_symbol_value` and `debug_dump_symbol_ihex` spawn one `savemem`
    through the commander with the same `--uid` and `--target` half as the
    three named tools, so the same wait reached them and the same refusal has
    to. The symbol is resolved offline first, from the ELF this service is told
    it flashed, so the spawn is the first thing that can fail.
    """
    config = load_config(str(write_config(tmp_path, debugger_type="pyocd", debugger_executable=FAKE_PYOCD_NO_PROBE, target_type="stm32f446re", gdb_executable=FAKE_GDB)))
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(elf_with_symbols([("boot_counter", 0x20000000, 4)]))
    service = AgenticHILToolService(config)
    try:
        # The ELF a flash would have proven on the target, set the way the
        # symbol-read suite sets it: a flash through this fixture refuses
        # before it can prove anything.
        service._symbol_elf = service.artifacts.validate_local_path("build/app.elf")["artifact"]
        started = time.perf_counter()
        result = service.call(tool, arguments)
        elapsed_s = time.perf_counter() - started
    finally:
        service.close()

    assert result["error_type"] != "timeout", result
    assert_refused_before_contact(result, config)
    assert result.get("retry_safe") is True, result
    # As above: the product's clock carries the claim where the result has
    # one, and the wall clock around the offline symbol read and the spawn
    # is held to twice the timeout.
    if "elapsed_ms" in result:
        assert result["elapsed_ms"] < scaled_time_bound(CONFIGURED_TIMEOUT_S) * 1000, result
    assert elapsed_s < scaled_time_bound(2 * CONFIGURED_TIMEOUT_S), (elapsed_s, result)
    log = log_of(config, result)
    assert "-W" in log["command"].split() or "--no-wait" in log["command"].split(), log["command"]
    assert log["stdout"] == RECORDED_NO_PROBE, log
    assert not (tmp_path / "build" / "boot_counter.hex").exists()


def test_a_pyocd_that_hangs_despite_the_flag_is_still_a_timeout_with_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The neighbour the flag must not move: a probe present and a target that never answers.

    `-W` only stops pyOCD waiting for a probe. A run that found one and then
    hung on the target is still reaped at `timeout_s`, still reports its
    hardware state unknown, still asks for a cleanup and still gets the two
    recovery resets after it, because nothing in that run said where it
    stopped. Pinned before the change and unchanged by it.
    """
    monkeypatch.setenv("AGENTIC_HIL_FAKE_PYOCD_HANGS_DESPITE_NO_WAIT", "1")
    config = config_for(tmp_path)
    service = AgenticHILToolService(config)
    try:
        result = service.call("reset_target", {"mode": "run"})
    finally:
        service.close()

    assert result["ok"] is False, result
    assert result["error_type"] == "timeout", result
    assert result["side_effect_status"] == "unknown", result
    assert result["retry_safe"] is False, result
    assert result["cleanup_required"] is True, result
    assert result.get("quarantine_id"), result
    assert result["recovery"]["attempted"] is True, result
    assert result["recovery"]["outcome"] == "failed", result
    log = log_of(config, result)
    assert log["timed_out"] is True, log
    # The call and the two recovery resets behind it, each reaped in turn.
    assert len(written_logs(config)) == 3, [path.name for path in written_logs(config)]


def test_probe_enumeration_is_spawned_exactly_as_before(tmp_path: Path) -> None:
    """The neighbour: `pyocd json --probes --no-config` connects to nothing and gets no flag.

    The fixture refuses any other argument list on `json`, the way the suite's
    other pyOCD fakes do, so a `-W` that leaked into the enumeration would turn
    this listing into `probe_discovery_failed`.
    """
    config = config_for(tmp_path)
    service = AgenticHILToolService(config)
    try:
        listed = service.call("debugger_probes_list")
    finally:
        service.close()

    assert listed["ok"] is True, listed
    assert listed["probes"] == [], listed
