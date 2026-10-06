"""pyOCD's own words for a flash it refused, said through the product on the board.

tests/fixtures/pyocd_failure_recordings.json holds what pyOCD 0.45.1 printed
when its flash was given a raw binary based past the end of the part's flash:
it refuses while it loads the file, before it programs anything, with `no
memory region defined for address <base>`. The unit suite replays that line and
pins the bucket it lands in. This module runs the same flash through
`agentic-hil mcp-stdio` on a copy of the tier's configuration that puts the
probe on `type: pyocd` with `flash_address` past the end of flash, and compares
the decisive line pyOCD printed with the recorded one, so a pyOCD that words the
refusal differently fails here before the replayed line goes stale.

Nothing here opens the probe or speaks to the board. The image is sixteen bytes
nobody can program, the flash is refused at load, and the module ends with the
demo put back on the board and running through the tier's own plan runner.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from agentic_hil.knowledge import remediation_fields

from .conftest import BENCH_ONLY, Bench, put_on_board
from .pyocd_recordings import TARGET_TYPE, pyocd_bench_environment, pyocd_provenance
from .test_bench_debug_sessions import McpServer, blocking_incident
from .test_bench_pyocd_sessions import PyocdBench

pytestmark = [pytest.mark.bench, BENCH_ONLY]

RECORDINGS = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "pyocd_failure_recordings.json").read_text(encoding="utf-8"))
OUTSIDE_FLASH = RECORDINGS["recordings"]["pyocd_flash_bin_outside_flash"]
# The base address the recording's argv carried: past the end of the part's
# flash and outside every region pyOCD's memory map for it holds.
BASE_ADDRESS = OUTSIDE_FLASH["argv"][OUTSIDE_FLASH["argv"].index("--base-address") + 1]
IMAGE = "build/pyocd-outside-flash.bin"
# pyOCD's log prefix: the milliseconds since it started, which differ per run.
ELAPSED = re.compile(r"^\d+ ")


def decisive_line(stderr: str) -> str:
    """The critical line pyOCD ended on, without the run's own elapsed time."""
    critical = [ELAPSED.sub("", line) for line in stderr.splitlines() if re.match(r"^\d+ C ", line)]
    assert len(critical) == 1, stderr
    return critical[0]


@pytest.fixture(scope="module")
def pyocd_bench(bench: Bench) -> Iterator[PyocdBench]:
    """A copy of the tier's configuration on `type: pyocd` with a flash address past the end of flash.

    Beside the configuration `init` wrote, under this session's own root, and
    removed afterwards. The tier's own configuration is never written."""
    executable, _, _ = pyocd_provenance(pyocd_bench_environment(bench))
    document = bench.configuration()
    entry = document["debuggers"][bench.debugger_name()]
    entry["type"] = "pyocd"
    entry["executable"] = executable
    entry["target_type"] = TARGET_TYPE
    entry["flash_address"] = BASE_ADDRESS
    entry.pop("interface_cfg", None)
    entry.pop("target_cfg", None)
    variant = bench.config.parent / "bench-pyocd-failures.yaml"
    assert not variant.exists(), f"refusing to overwrite an existing fixture config: {variant}"
    variant.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    image = bench.project / IMAGE
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(bytes(range(16)))
    try:
        yield PyocdBench(project=bench.project, config=variant, config_root=bench.config_root, state_root=bench.state_root)
    finally:
        image.unlink(missing_ok=True)
        variant.unlink(missing_ok=True)


@pytest.fixture(scope="module", autouse=True)
def the_demo_runs_afterwards(bench: Bench, firmware: Path) -> Iterator[None]:
    """The demo back on the board and running once this module is done with it."""
    yield
    report = put_on_board(bench, firmware)
    if report.get("ok") is not True:
        pytest.fail(f"the demo firmware could not be put back on the board after the pyOCD failures: {report.get('summary')}", pytrace=False)


def test_a_bin_based_past_the_end_of_flash_is_refused_in_the_recorded_words_and_classified_flash_failed(pyocd_bench: PyocdBench) -> None:
    server = McpServer(pyocd_bench)
    try:
        result = server.tool("flash_firmware", {"image_path": IMAGE})
    finally:
        server.shut_down()
        # The refused flash stands as an incident, as every flash_failed does:
        # the product cannot tell from the words alone that nothing was written.
        # pyOCD refused while loading the file, before it programmed anything,
        # and the demo goes back on the board after the module.
        incident = blocking_incident(pyocd_bench)
        if incident is not None:
            recovered = pyocd_bench.run("recover", "--confirm-safe-state", "--quarantine-id", str(incident.get("quarantine_id") or ""))
            assert recovered.returncode == 0, f"{recovered.stdout}{recovered.stderr}"

    assert result["ok"] is False, result
    assert result["backend"] == "pyocd", result
    assert result["backend_error_type"] == "flash_failed", result
    assert result["error_type"] == "flash_failed", result
    assert result.get("remediation") == remediation_fields("flash_failed", "pyocd").get("remediation"), result.get("remediation")
    output = result["programmer_output"]
    assert output["returncode"] == OUTSIDE_FLASH["returncode"], output
    assert decisive_line(output["stderr"]) == decisive_line(OUTSIDE_FLASH["stderr"]), output
