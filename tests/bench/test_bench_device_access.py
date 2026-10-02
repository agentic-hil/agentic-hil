"""`doctor`'s device-access check on a bench whose account may open the probe and the port.

The stage without the device group holds the refusal; this is the other half,
on the tier's ordinary run: the check finds the probe's USB node through sysfs
and the serial port's real node behind the configured path, and passes both.
It asks the kernel and opens nothing, and so does every assertion here. The
port is the probe's own, and then the USB-UART adapter's beside it.
"""

from __future__ import annotations

import os

import pytest

from .conftest import BENCH_ONLY, OVER_BOTH_LINES, Bench, bench_over

pytestmark = [pytest.mark.bench, BENCH_ONLY]


@pytest.fixture(scope="module", params=OVER_BOTH_LINES)
def bench(request: pytest.FixtureRequest, configured_bench: Bench) -> Bench:
    """The check once with the probe's own port, and once with the USB-UART adapter's."""
    return bench_over(request, configured_bench)


def test_doctor_passes_the_device_access_check_for_the_probe_and_the_port(bench: Bench) -> None:
    code, report = bench.document("doctor")

    assert code == 0, report
    assert "device_access" not in report["unhealthy"], report["unhealthy"]
    probe = report["debuggers"][bench.debugger_name()]["device_access"]
    port = report["com_ports"][bench.com_port_name()]["device_access"]
    configured = bench.configuration()["com_ports"][bench.com_port_name()]["device"]
    assert probe["node"].startswith("/dev/bus/usb/"), probe
    assert port["node"] == os.path.realpath(configured), port
    for check in (probe, port):
        assert check["ok"] is True, check
        assert "error_type" not in check, check
        assert check["node"] in check["summary"], check["summary"]
        # The kernel's own answer for this process, asked the way the check asks it.
        assert os.access(check["node"], os.R_OK | os.W_OK), check
