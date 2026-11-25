"""The delivery checks follow their changelog entries across a release cut."""

import re
from importlib import import_module
from pathlib import Path

import pytest

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
UNRELEASED = "## [Unreleased]"
DATED_RELEASE = re.compile(r"## \[\d+\.\d+\.\d+\] - \d{4}-\d{2}-\d{2}")
BULLET = re.compile(r"^-[ \t]+[^\n]*(?:\n[ \t]+[^\n]*)*\n?", re.MULTILINE)
CHECKS = {
    "hardfault": (
        "test_f446re_hardfault_plan.test_recorded_bench_release", "(#921)", "Describe the handler stop",
    ),
    "defmt": (
        "test_defmt_serial_bench.test_unreleased_describes_board_observations",
        "(#813)",
        "must describe the three Nucleo-F446RE",
    ),
    "pico": (
        "test_pico_rp2040_demo.test_repository_demo_delivery", "(#919)", "document and package the Pico demo",
    ),
    "picotool": (
        "test_picotool_backend.test_published_picotool_configuration_and_support_contract",
        "Add picotool 2 support",
        "published picotool configuration and support contract is incomplete",
    ),
    "bench-stage": (
        "test_bench_in_container.test_unreleased_changed_records_issue_800_once",
        "(#800)",
        "must contain one entry ending in",
    ),
}


def _entry(changelog: str, marker: str) -> re.Match[str]:
    (entry,) = (entry for entry in BULLET.finditer(changelog) if marker in entry[0])
    return entry


def _release_changelog(marker: str, *, reopened: bool) -> str:
    changelog = CHANGELOG.read_text(encoding="utf-8")
    sections = re.split(r"(?m)(?=^##[ \t]+)", changelog)
    for index, section in enumerate(sections):
        heading, newline, body = section.partition("\n")
        heading = heading.rstrip()
        if (heading == UNRELEASED or DATED_RELEASE.fullmatch(heading)) and any(
            marker in bullet[0] for bullet in BULLET.finditer(body)
        ):
            owner_index = index
            break
    else:
        raise AssertionError(f"No changelog release section owns the entry marked {marker!r}")

    if heading == UNRELEASED:
        # Keep the synthetic version out of the repository's release-pin inventory.
        version = ".".join(map(str, (0, 29, 0)))
        section = f"## [{version}] - 2026-11-24" + newline + body
    if reopened:
        section = UNRELEASED + "\n\n" + section
    return "".join(
        section if offset == owner_index else original
        for offset, original in enumerate(sections)
        if offset == owner_index or original.partition("\n")[0].rstrip() != UNRELEASED
    )


def _redirect_changelog(monkeypatch: pytest.MonkeyPatch, changelog: str) -> None:
    read_text = Path.read_text

    def read_changelog(path: Path, *args, **kwargs) -> str:
        if path == CHANGELOG:
            return changelog
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_changelog)


def _run_check(check: str) -> None:
    # Imported when the check runs, so collecting this file imports no other test module.
    module, function = check.rsplit(".", 1)
    getattr(import_module(module), function)()


@pytest.mark.parametrize("layout", ["cut", "reopened"])
@pytest.mark.parametrize("check_name", list(CHECKS))
def test_release_layout(monkeypatch: pytest.MonkeyPatch, check_name: str, layout: str) -> None:
    check, marker, _ = CHECKS[check_name]
    _redirect_changelog(monkeypatch, _release_changelog(marker, reopened=layout == "reopened"))
    _run_check(check)


@pytest.mark.parametrize("check_name", list(CHECKS))
def test_reopened_release_rejects_missing_entry(monkeypatch: pytest.MonkeyPatch, check_name: str) -> None:
    check, marker, message = CHECKS[check_name]
    changelog = _release_changelog(marker, reopened=True)
    entry = _entry(changelog, marker)
    _redirect_changelog(monkeypatch, changelog[:entry.start()] + changelog[entry.end():])
    with pytest.raises(AssertionError, match=message):
        _run_check(check)


def test_reopened_release_rejects_second_bench_stage_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    check, marker, message = CHECKS["bench-stage"]
    changelog = _release_changelog(marker, reopened=True)
    entry = _entry(changelog, marker)
    doubled = changelog.replace(f"{UNRELEASED}\n", f"{UNRELEASED}\n\n### Changed\n\n{entry[0]}", 1)
    assert doubled != changelog
    _redirect_changelog(monkeypatch, doubled)
    with pytest.raises(AssertionError, match=message):
        _run_check(check)


def test_reopened_release_rejects_missing_hardfault_forced_bit(monkeypatch: pytest.MonkeyPatch) -> None:
    check, marker, _ = CHECKS["hardfault"]
    changelog = _release_changelog(marker, reopened=True)
    entry = _entry(changelog, marker)
    incomplete = entry[0].replace("`HFSR.FORCED`", "", 1)
    assert incomplete != entry[0]
    _redirect_changelog(monkeypatch, changelog[:entry.start()] + incomplete + changelog[entry.end():])
    with pytest.raises(AssertionError, match=r"Name HFSR\.FORCED"):
        _run_check(check)
