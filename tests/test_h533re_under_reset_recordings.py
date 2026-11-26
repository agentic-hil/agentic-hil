"""Offline acceptance for the NUCLEO-H533RE shared-bench first-flash evidence."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

import pytest
from conftest import write_config

from agentic_hil.backends.common import command_for_log
from agentic_hil.backends.stlink import STLINK_SUCCESS_CONFIRMATION, STLinkBackend
from agentic_hil.config import load_config
from agentic_hil.knowledge import remediation_fields
from agentic_hil.test_reactor import load_test_config
from tests.bench.serve_flash_recordings import check_host_privacy, record_job
from tests.support import unredacted_serials

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/stm32cubeprogrammer_2_23_0_h533re_under_reset_recordings.json"
WATCHDOG_PLAN = ROOT / "tests/hil/h533re_first_flash_shared_bench.yaml"
CONTROL_PLAN = ROOT / "tests/hil/h533re_hotplug_control_shared_bench.yaml"
SOURCE = ROOT / "tests/bench/firmware/h533re_under_reset.c"
LINKER = ROOT / "tests/bench/firmware/h533re_under_reset.ld"
DOWNLOAD_ERRORS = ("Error: failed to download  Sector[0]", "Error: failed to download the File")
VERIFY_ERRORS = ("Error: Data mismatch found", "Error: Download verification failed")
WATCHDOG_ON = re.compile(r"^ahil752 image=previous-iwdg boot_rsr=[0-9a-f]{8} live_rsr=00000000 watchdog=on ")
PROGRAMMER = "/opt/st/stm32cubeclt_1.22.0/STM32CubeProgrammer/bin/STM32_Programmer_CLI"


@pytest.fixture
def evidence() -> dict:
    return json.loads(FIXTURE.read_bytes())


@pytest.fixture
def backend(tmp_path: Path) -> STLinkBackend:
    return STLinkBackend(load_config(str(write_config(tmp_path, debugger_type="stlink"))))


def _first_flashes(recordings: dict, mode: str) -> dict:
    return {name: job for name, job in recordings.items() if name.startswith(f"{mode}_first_flash_")}


def _actions(events: list[dict]) -> list[str]:
    return [event.get("step", "uart") for event in events]


def _assert_verified(backend: STLinkBackend, flash: dict) -> None:
    assert flash["returncode"] == 0 and flash["timed_out"] is False
    assert backend._backend_error_from_output(flash["stdout"] + flash["stderr"], "flash_firmware") is None
    assert backend._confirm_operation_success(flash["stdout"], STLINK_SUCCESS_CONFIRMATION["flash_firmware"])["confirmed"]


def test_recording_shape_provenance_and_image_identity(evidence: dict) -> None:
    assert set(evidence) == {
        "what", "recorded_on", "bench", "tool_versions", "connect_mode_reader",
        "reset_flags", "limits", "redaction", "memory_map", "firmware", "recordings",
    }
    assert evidence["recorded_on"] == "2026-11-25"
    assert all(part in evidence["bench"] for part in ("NUCLEO-H533RE", "STLINK-V3", "V3J13M4", "Linux", "agentic-hil serve"))
    assert evidence["tool_versions"] == {"stm32cubeprogrammer": "2.23.0", "stm32cubeclt": "1.22.0"}
    firmware = evidence["firmware"]
    assert set(firmware) == {"source", "linker_script", "compiler", "flags", "images"}
    assert firmware["source"] == SOURCE.relative_to(ROOT).as_posix()
    assert firmware["linker_script"] == LINKER.relative_to(ROOT).as_posix()
    assert firmware["compiler"] == "arm-none-eabi-gcc (GNU Tools for STM32 14.3.rel1.20251027-0700) 14.3.1 20250623"
    assert firmware["flags"] == [
        "-mcpu=cortex-m33", "-mthumb", "-mfloat-abi=soft", "-O1", "-g0", "-std=gnu11",
        "-Wall", "-Wextra", "-Werror", "-ffreestanding", "-nostdlib", "-nostartfiles",
        "-Wl,--build-id=none", "-Wl,--fatal-warnings",
    ]
    images = firmware["images"]
    assert set(images) == {"next", "previous_systick", "previous_iwdg"}
    for selector, image in enumerate(("next", "previous_systick", "previous_iwdg")):
        assert set(images[image]) == {"file", "define", "sha256"}
        assert images[image]["file"] == f"h533re_under_reset_{image}.elf"
        assert images[image]["define"] == f"PREVIOUS={selector}"
        assert re.fullmatch(r"[0-9a-f]{64}", images[image]["sha256"])

    image_digests = {entry["file"]: entry["sha256"] for entry in images.values()}
    assert len(set(image_digests.values())) == 3
    recordings = evidence["recordings"]
    assert len(recordings) == len({record["job_id"] for record in recordings.values()}) == 16
    assert {record["agentic_hil"] for record in recordings.values()} == {"0.29.0"}
    configurations: dict[str, set[str]] = {"hot_plug": set(), "under_reset": set()}
    for name, record in recordings.items():
        assert set(record) == {"job_id", "agentic_hil", "bench_config", "plan_sha256", "outcome", "events"}
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", record["bench_config"]), name
        group = "hot_plug" if name.startswith("hot_plug_") else "under_reset"
        configurations[group].add(record["bench_config"])
        flashes = [event for event in record["events"] if event.get("step") == "flash"]
        common = {"step", "image", "image_sha256", "argv", "returncode"}
        assert set(flashes[-1]) == common | {"timed_out", "stdout", "stderr"}
        for flash in flashes[:-1]:
            assert set(flash) == common
        for flash in flashes:
            assert flash["image_sha256"] == image_digests[flash["image"]], name
    assert all(len(group) == 1 for group in configurations.values())
    assert configurations["hot_plug"].isdisjoint(configurations["under_reset"])


def _strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def test_flash_modes_serial_redaction_and_host_privacy(evidence: dict) -> None:
    for value in _strings(evidence):
        check_host_privacy(value)
    for name, job in evidence["recordings"].items():
        flash_mode = "HOTPLUG" if name.startswith("hot_plug_") else "UR"
        modes = {"flash": flash_mode, "reset": "NORMAL", "read_symbol": "HOTPLUG"}
        for event in job["events"]:
            if "argv" not in event:
                continue
            argv = event["argv"]
            assert [arg for arg in argv if arg.startswith("mode=")] == [f"mode={modes[event['step']]}"], name
            assert [arg for arg in argv if arg.lower().startswith("sn=")] == ["sn=[redacted]"], name
            assert not unredacted_serials({"stdout": "", "stderr": "", **event}), name


def test_first_flash_outcomes_and_backend_classification(evidence: dict, backend: STLinkBackend) -> None:
    recordings = evidence["recordings"]
    hot_plug = _first_flashes(recordings, "hot_plug")
    under_reset = _first_flashes(recordings, "under_reset")
    assert (len(hot_plug), len(under_reset)) == (6, 5)
    observed: Counter[str] = Counter()
    for name, record in (hot_plug | under_reset).items():
        events = record["events"]
        assert _actions(events[:4]) == ["flash", "reset", "uart", "flash"], name
        assert events[0]["image"] == evidence["firmware"]["images"]["previous_iwdg"]["file"], name
        assert events[0]["returncode"] == events[1]["returncode"] == 0, name
        assert WATCHDOG_ON.match(events[2]["uart"]), name
        flash = events[3]
        assert flash["image"] == evidence["firmware"]["images"]["next"]["file"], name
        assert "-rst" not in flash["argv"], name
        if name in under_reset:
            assert record["outcome"] == "success", name
            _assert_verified(backend, flash)
            assert _actions(events[4:]) == ["read_symbol", "reset", "uart"], name
            assert events[4]["symbol"] == "rcc_rsr" and events[4]["value"] == "0x04000000", name
            assert "image=next boot_rsr=14000000 live_rsr=00000000 watchdog=off " in events[-1]["uart"], name
            continue

        assert record["outcome"] == "failure" and flash["returncode"] == 1 and flash["timed_out"] is False, name
        assert _actions(events[4:]) in ([], ["uart"]), name
        transcript = flash["stdout"] + flash["stderr"]
        expected = "flash_failed" if DOWNLOAD_ERRORS[0] in transcript else "verify_failed"
        markers = DOWNLOAD_ERRORS if expected == "flash_failed" else VERIFY_ERRORS
        assert all(marker in transcript for marker in markers), name
        assert backend._backend_error_from_output(transcript, "flash_firmware") == expected, name
        observed[expected] += 1
    assert observed == {"flash_failed": 3, "verify_failed": 3}


def test_retries_control_and_reset_flags_in_event_order(evidence: dict, backend: STLinkBackend) -> None:
    recordings = evidence["recordings"]
    retries = {
        "hot_plug_retry_after_verify_mismatch": (["flash", "uart", "read_symbol", "reset"], "0x00000000"),
        "hot_plug_retry_after_download_failure": (["flash", "read_symbol", "reset", "uart"], "0x34000000"),
    }
    next_image = evidence["firmware"]["images"]["next"]["file"]
    for name, (actions, flags) in retries.items():
        job = recordings[name]
        events = job["events"]
        assert job["outcome"] == "success"
        assert _actions(events) == actions
        assert events[0]["image"] == next_image
        assert next(event["value"] for event in events if event.get("step") == "read_symbol") == flags
        assert "image=next boot_rsr=34000000 live_rsr=00000000 watchdog=off " in next(
            event["uart"] for event in events if "uart" in event
        )
        _assert_verified(backend, events[0])

    control = recordings["hot_plug_control"]
    events = control["events"]
    assert control["outcome"] == "success"
    assert _actions(events) == ["flash", "reset", "uart", "flash", "read_symbol", "reset", "uart"]
    assert events[0]["image"] == evidence["firmware"]["images"]["previous_systick"]["file"]
    assert "image=previous-systick boot_rsr=14000000 live_rsr=00000000 watchdog=off " in events[2]["uart"]
    assert events[3]["image"] == next_image and "-rst" not in events[3]["argv"]
    _assert_verified(backend, events[3])
    assert events[4]["symbol"] == "rcc_rsr" and events[4]["value"] == "0x00000000"
    assert "image=next boot_rsr=14000000 live_rsr=00000000 watchdog=off " in events[-1]["uart"]

    for mode, previous, watchdog, flags in (
        ("hot_plug", "previous-systick", "off", "14000000"),
        ("under_reset", "previous-iwdg", "on", "04000000"),
    ):
        job = recordings[f"{mode}_reset_after_flash"]
        assert job["outcome"] == "success" and _actions(job["events"]) == ["uart", "flash", "uart"]
        before, flash, boot = job["events"]
        assert f"image={previous} boot_rsr=14000000 live_rsr=00000000 watchdog={watchdog} " in before["uart"]
        assert flash["image"] == next_image and "-rst" in flash["argv"]
        _assert_verified(backend, flash)
        assert f"image=next boot_rsr={flags} live_rsr=00000000 watchdog=off " in boot["uart"]


def test_version_five_plans_load_with_recorded_steps_and_hashes(evidence: dict) -> None:
    recordings = evidence["recordings"]
    watchdog_jobs = _first_flashes(recordings, "hot_plug") | _first_flashes(recordings, "under_reset")
    images = evidence["firmware"]["images"]
    for path, jobs, previous, watchdog, flags in (
        (WATCHDOG_PLAN, watchdog_jobs.values(), "previous_iwdg", "on", 0x04000000),
        (CONTROL_PLAN, [recordings["hot_plug_control"]], "previous_systick", "off", 0),
    ):
        assert "version: 5" in path.read_text(encoding="utf-8").splitlines()
        plan = load_test_config(str(path), str(ROOT))
        assert {job["plan_sha256"] for job in jobs} == {plan.sha256}
        assert [step.action for step in plan.steps] == [
            "flash", "uart_open", "reset", "uart_expect", "flash", "read_symbol", "reset", "uart_expect", "uart_close",
        ]
        assert [step.route for step in plan.steps] == [
            "stlink_v3_b", "stlink_v3_b_vcp", "stlink_v3_b", "stlink_v3_b_vcp", "stlink_v3_b",
            "stlink_v3_b", "stlink_v3_b", "stlink_v3_b_vcp", "stlink_v3_b_vcp",
        ]
        for index, image in ((0, previous), (4, "next")):
            assert plan.steps[index].arguments == {"image_path": f"build/hil/{images[image]['file']}", "reset_after_flash": False}
        assert plan.steps[1].arguments == {"clear_buffer": True}
        assert plan.steps[2].arguments == plan.steps[6].arguments == {"mode": "run"}
        assert plan.steps[3].arguments == {
            "pattern": f"ahil752 image={previous.replace('_', '-')} boot_rsr=[0-9a-f]{{8}} live_rsr=00000000 watchdog={watchdog}",
            "timeout_s": 10,
        }
        assert plan.steps[5].arguments == {"symbol": "rcc_rsr", "size_bytes": 4, "comparator": {"equals": flags}}
        assert plan.steps[7].arguments == {
            "pattern": "ahil752 image=next boot_rsr=14000000 live_rsr=00000000 watchdog=off", "timeout_s": 10,
        }


def test_firmware_register_boundary() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert dict(re.findall(r"^#define (\w+) REGISTER\((0x[0-9A-F]+)U\)$", source, re.MULTILINE)) == {
        "RCC_AHB2ENR": "0x44020C8C", "RCC_APB1LENR": "0x44020C9C", "RCC_RSR": "0x44020CF4",
        "GPIOA_MODER": "0x42020000", "GPIOA_AFRL": "0x42020020",
        "USART2_CR1": "0x40004400", "USART2_BRR": "0x4000440C", "USART2_ISR": "0x4000441C", "USART2_TDR": "0x40004428",
        "IWDG_KR": "0x40003000", "IWDG_PR": "0x40003004", "IWDG_RLR": "0x40003008", "IWDG_SR": "0x4000300C",
        "SYST_CSR": "0xE000E010", "SYST_RVR": "0xE000E014", "SYST_CVR": "0xE000E018",
    }
    assert re.findall(r"\.set\s+(\w+),\s*(0x[0-9A-F]+)", source) == [("rcc_rsr", "0x44020CF4")]
    assert source.count("REGISTER(") == 17


_STAGING = "/tmp/agentic-hil-artifacts-example/stage one"
_READ_STAGING = "/tmp/agentic-hil-symbol-value-example"
_SERIAL = "ExampleProbeA12"
_SUMMARY = {
    "tools": {"agentic_hil": "0.29.0"},
    "plan": {"sha256": "e" * 64},
    "bench": {"config_digest": "sha256:" + "d" * 64},
    "outcome": "success",
}


def _timestamp(second: int) -> str:
    return f"2026-11-25T16:29:{second:02d}.000Z"


def _command(mode: str, serial: str, *args: str) -> list[str]:
    return [PROGRAMMER, "-q", "-c", "port=SWD", f"mode={mode}", *([f"sn={serial}"] if serial else []), *args]


def _fetched_job(
    folder: Path, *, serial: str = _SERIAL, other_serial: str | None = None, extra_stdout: str = ""
) -> None:
    folder.mkdir()
    steps = []
    logs = {"run-summary.json": json.dumps(_SUMMARY)}

    def step(action: str, **result) -> None:
        steps.append({"index": len(steps) + 1, "action": action, "result": result})

    def programmer(action: str, second: int, argv: list[str], stdout: str = "", stderr: str = "", **result) -> None:
        tool = {"flash": "flash_firmware", "reset": "reset_target", "read_symbol": "debug_symbol_value"}[action]
        name = f"stlink-20261125T1629{second:02d}000Z-{tool}.log"
        logs[f"logs/{name}"] = json.dumps(
            {"command": command_for_log(argv), "returncode": 0, "stdout": stdout, "stderr": stderr, "timed_out": False}
        )
        step(action, started_at=_timestamp(second), finished_at=_timestamp(second + 1), log_path=f".agentic-hil/logs/{name}", **result)

    programmer(
        "flash", 1, _command("UR", serial, "-w", f"{_STAGING}/previous.elf", "-v"),
        "Initial verification\n", artifact={"path": "build/hil/previous.elf", "sha256": "a" * 64},
    )
    uart_sessions = {
        port: {"log_path": f".agentic-hil/logs/com-20261125T162902000Z-{port}.jsonl"}
        for port in ("dut_vcp", "aux_vcp")
    }
    for port, session in uart_sessions.items():
        step("uart_open", port_id=port, session=session)
    programmer("reset", 3, _command("NORMAL", serial, "-rst"))
    step("uart_expect", port_id="dut_vcp", bytes_received=32)
    programmer(
        "flash", 7, _command("UR", serial.upper(), "-w", f"{_STAGING}/next.elf", "-v"),
        (f"ST-LINK SN  : {other_serial or serial.lower()}\n" if other_serial or serial else "")
        + f"Using {_STAGING}/next.elf\nDownload verified successfully\n{extra_stdout}",
        f"probe {serial.title()}\n" if serial else "",
        artifact={"path": "build/hil/next.elf", "sha256": "b" * 64},
    )
    programmer(
        "read_symbol", 9, _command("HOTPLUG", serial, "-r", "0x44020cf4", "4", f"{_READ_STAGING}/symbol-value.hex"),
        symbol="rcc_rsr", value_unsigned=0x04000000,
    )
    step("read_symbol", symbol="second", value_unsigned=0)
    for port, session in uart_sessions.items():
        step("uart_close", port_id=port, session=session)

    for port, device, chunks in (
        ("dut_vcp", "/dev/ttyACM0", [
            (4, "rx", "older\r\nprevious watch"), (5, "rx", "dog=on\r"), (6, "rx", "\n"),
            (6, "tx", "host output\n"), (10, "rx", "next ready\r\nunfinished"),
        ]),
        ("aux_vcp", "/dev/ttyACM1", [(5, "rx", "during "), (7, "rx", "download\n")]),
    ):
        entries = [json.dumps({"time": _timestamp(2), "event": "start", "port_id": port, "device": device})]
        for second, direction, data in chunks:
            payload = data.encode()
            entries.append(json.dumps({
                "time": _timestamp(second), "direction": direction, "bytes": len(payload), "hex": payload.hex(), "text": data,
            }))
        name = uart_sessions[port]["log_path"].rsplit("/", 1)[-1]
        logs[f"logs/{name}"] = "\n".join(entries) + "\n"
    (folder / "report.json").write_text(json.dumps({"job_id": "job-synthetic", "steps": steps}), encoding="utf-8")
    with ZipFile(folder / "job-synthetic-evidence.zip", "w") as archive:
        for name, data in logs.items():
            archive.writestr(name, data)


def test_record_job_preserves_order_values_and_redacted_transcripts(tmp_path: Path) -> None:
    fetched = tmp_path / "job"
    _fetched_job(fetched)
    redacted = "[redacted]"
    assert record_job(fetched) == {
        "job_id": "job-synthetic",
        "agentic_hil": _SUMMARY["tools"]["agentic_hil"],
        "bench_config": _SUMMARY["bench"]["config_digest"],
        "plan_sha256": _SUMMARY["plan"]["sha256"],
        "outcome": "success",
        "events": [
            {
                "step": "flash", "image": "previous.elf", "image_sha256": "a" * 64,
                "argv": _command("UR", redacted, "-w", "[staging]/previous.elf", "-v"), "returncode": 0,
            },
            {"step": "reset", "argv": _command("NORMAL", redacted, "-rst"), "returncode": 0},
            {"uart": "previous watchdog=on"},
            {
                "step": "flash", "image": "next.elf", "image_sha256": "b" * 64,
                "argv": _command("UR", redacted, "-w", "[staging]/next.elf", "-v"), "returncode": 0,
                "timed_out": False, "stdout": "ST-LINK SN  : [redacted]\nUsing [staging]/next.elf\nDownload verified successfully\n",
                "stderr": "probe [redacted]\n",
            },
            {"uart": "during download"},
            {
                "step": "read_symbol", "symbol": "rcc_rsr", "value": "0x04000000", "returncode": 0,
                "argv": _command("HOTPLUG", redacted, "-r", "0x44020cf4", "4", "[staging]/symbol-value.hex"),
            },
            {"step": "read_symbol", "symbol": "second", "value": "0x00000000"},
            {"uart": "next ready"},
        ],
    }


def test_record_job_refuses_invalid_bundles_and_private_output(tmp_path: Path) -> None:
    empty = tmp_path / "missing-archive"
    empty.mkdir()
    with pytest.raises(ValueError):
        record_job(empty)

    duplicate = tmp_path / "duplicate-archive"
    _fetched_job(duplicate)
    (duplicate / "duplicate-evidence.zip").write_bytes((duplicate / "job-synthetic-evidence.zip").read_bytes())
    with pytest.raises(ValueError):
        record_job(duplicate)

    for name, options in (
        ("missing-probe", {"serial": ""}),
        ("ambiguous-probe", {"other_serial": "OtherProbeA12"}),
        ("home-path", {"extra_stdout": "from /home/bench/private.elf\n"}),
        ("home-uri", {"extra_stdout": "from file:///home/bench/private.elf\n"}),
        ("temporary-path", {"extra_stdout": "from /tmp/other/private.elf\n"}),
        ("device-path", {"extra_stdout": "via /dev/ttyACM0\n"}),
        ("host-address", {"extra_stdout": "via 192.0.2.34\n"}),
        ("sentence-address", {"extra_stdout": "via 192.0.2.34.\n"}),
    ):
        fetched = tmp_path / name
        _fetched_job(fetched, **options)
        with pytest.raises(ValueError):
            record_job(fetched)


def _watchdog_results_and_limits(text: str) -> bool:
    """Check the evidence claims without prescribing a complete sentence."""
    wording = " ".join(text.lower().replace("\x60", "").split())
    result_markers = re.compile(
        r"\b(?:(?P<hot>hot[ -]plug)|(?P<under>under[ _-]reset)|(?P<failed>failed|failures?)|(?P<verified>verified)"
        r"|(?P<six>6\s*(?:of|/)\s*6|six of six)|(?P<five>5\s*(?:of|/)\s*5|five of five))\b"
    )
    sentences = re.split(r"[.;]", wording)
    claims = set()
    for sentence in sentences:
        markers = [match.lastgroup for match in result_markers.finditer(sentence)]
        # Match neighboring markers so one outcome cannot borrow the other's
        # count or mode.
        claims.update(frozenset(group) for group in zip(markers, markers[1:], markers[2:], strict=False))
    recorded_nrst = any(
        re.search(r"\bnucleo-h533re\b|\b(?:this|that|the recorded) board\b", sentence)
        and re.search(r"\bnrst\b", sentence)
        and (
            re.search(r"\b(?:drove|driven|drives)\b", sentence)
            or (re.search(r"\breset\b", sentence) and re.search(r"\brecorded\b", sentence))
        )
        for sentence in sentences
    )
    return (
        all(marker in wording for marker in ("nucleo-h533re", "watchdog", "stm32cubeprogrammer", "st-link"))
        and re.search(r"\bfirst[ -]flash", wording) is not None
        and frozenset({"hot", "six", "failed"}) in claims
        and frozenset({"under", "five", "verified"}) in claims
        and recorded_nrst
        and re.search(r"\b(?:one |single )board\b", wording) is not None
        and re.search(r"\b(?:one |single )st-link firmware\b", wording) is not None
        and re.search(r"\b(?:one |single )(?:stm32)?cubeprogrammer version\b", wording) is not None
        and re.search(r"\bcause\b[^.]*\b(?:unknown|unestablished|not (?:known|established))\b", wording) is not None
    )


def test_published_guidance_matches_recorded_watchdog_results() -> None:
    remediation = remediation_fields("flash_failed", "stlink")["remediation"]
    setting = next(step for step in remediation if "debuggers.<name>.connect_mode" in step)
    if not _watchdog_results_and_limits(setting):
        raise AssertionError("ST-LINK flash guidance lacks the recorded H533RE watchdog outcomes and NRST evidence")

    docs = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    section = docs.split("## Connecting Under Reset", 1)[1].split("\n## ", 1)[0]
    assert _watchdog_results_and_limits(section), "Connecting Under Reset lacks the board-specific watchdog results and limits"
    assert re.search(r"systick|1\s*khz|interrupt", section, re.IGNORECASE)
    assert re.search(r"without (?:a |the )watchdog|watchdog[- ]off|no watchdog", section, re.IGNORECASE)
    assert re.search(r"only .?flash_firmware.? reads", section, re.IGNORECASE)
    assert not re.search(r"(?<![\d.])0\.29\.0(?![\d.])", setting + section)

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert sum(line.startswith("- ") and line.rstrip().endswith("(#752)") for line in changelog.splitlines()) == 1


def test_checkout_only_recording_module_is_excluded() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    module = Path(__file__).relative_to(ROOT).as_posix()
    if not any(line.split() == ["exclude", module] for line in manifest.splitlines()):
        raise AssertionError("MANIFEST.in lacks the exclusion for the H533RE recording test")
