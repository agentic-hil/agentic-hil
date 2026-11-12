"""Pin the recorded ST-Link download guidance in the shipped documents."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_configuration_names_download_failure_and_limits() -> None:
    text = (REPOSITORY_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    section = text.split("## Connecting Under Reset", 1)[1].split("\n## ", 1)[0]
    required = (
        "Error: failed to download  Sector[0]",
        "Error: failed to download the File",
        "`under_reset` was not measured for this download case",
        "the out-of-range recording already used `mode=UR`",
    )

    if not all(phrase in section for phrase in required):
        raise AssertionError("Connecting Under Reset lacks the recorded download failure and its limits")


def test_configuration_removes_unverified_nucleo_nrst_claim() -> None:
    text = (REPOSITORY_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")

    if "On a Nucleo with its on-board ST-Link it already does" in text:
        raise AssertionError("Connecting Under Reset still asserts unverified Nucleo NRST wiring")


def test_troubleshooting_names_flash_failed_output_and_remediation() -> None:
    troubleshooting = (REPOSITORY_ROOT / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    changelog = (REPOSITORY_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    section = (
        troubleshooting.split("## 10.", 1)[1]
        .split("\n", 1)[1]
        .split("\n## 10a.", 1)[0]
    )
    guidance_tokens = ("`flash_failed`", "`programmer_output`", "`remediation`")
    has_guidance = any(
        all(token in paragraph for token in guidance_tokens)
        for paragraph in section.split("\n\n")
    )

    unreleased = changelog.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    fixed = unreleased.split("### Fixed", 1)[1].split("\n### ", 1)[0]
    issue_bullets = [
        line for line in fixed.splitlines() if line.startswith("- ") and line.endswith("(#751)")
    ]

    if not has_guidance or len(issue_bullets) != 1:
        raise AssertionError("TROUBLESHOOTING section 10 guidance or CHANGELOG entry for #751 is missing")
