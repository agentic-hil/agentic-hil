"""Read-only software check for pyOCD's Nucleo-F446RE CMSIS-pack support.

This module is selected explicitly from a container image without USB devices.
The pyocd JSON target listing does not enumerate or open probes; it reads the
installed target database. A separate pack listing verifies the installed DFP
and reports its actual version.
"""

from __future__ import annotations

import json
import shutil
import subprocess


def test_pyocd_reports_both_f446_target_types_from_an_installed_pack() -> None:
    pyocd = shutil.which("pyocd")
    assert pyocd is not None, "the software-check image must include pyOCD"

    targets_result = subprocess.run(
        [pyocd, "json", "--targets", "--no-config"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    packs_result = subprocess.run(
        [pyocd, "pack", "show"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert targets_result.returncode == 0, (targets_result.stdout, targets_result.stderr)
    assert packs_result.returncode == 0, (packs_result.stdout, packs_result.stderr)

    target_document = json.loads(targets_result.stdout)
    assert target_document["pyocd_version"] == "0.45.1", target_document
    targets = {
        entry["name"]: entry
        for entry in target_document["targets"]
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    for name, part_number in (
        ("stm32f446re", "STM32F446RE"),
        ("stm32f446retx", "STM32F446RETx"),
    ):
        assert name in targets, (name, targets_result.stdout)
        assert targets[name]["part_number"] == part_number, targets[name]
        assert targets[name]["source"] == "pack", targets[name]

    pack_versions = []
    for line in packs_result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "Keil.STM32F4xx_DFP":
            pack_versions.append(fields[1])
    assert len(pack_versions) == 1, packs_result.stdout
    # pyOCD's supported pack-install command selects the vendor index's
    # current release rather than accepting a version selector. Keep that
    # resolution visible and fail deliberately if the index advances; updating
    # this expectation then requires reviewing the changed DFP version.
    assert pack_versions == ["3.1.1"], packs_result.stdout
