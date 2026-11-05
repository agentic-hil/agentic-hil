"""The nightly measures both filesystems and retires images without force."""

from __future__ import annotations

import shlex
import subprocess
from types import SimpleNamespace

import pytest

from tools import bench_image_store as store


@pytest.mark.parametrize("temporary", [None, "temporary-storage"])
def test_reporting_shows_both_places_in_gib_and_the_runtime_summary_without_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], temporary: str | None,
) -> None:
    commands = []
    asked = []
    if temporary:
        monkeypatch.setenv("TMPDIR", temporary)
    else:
        monkeypatch.delenv("TMPDIR", raising=False)

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "private-store\n", "")

    def usage(place: str) -> SimpleNamespace:
        asked.append(place)
        return SimpleNamespace(free=(8 if len(asked) == 1 else 7) * 1024**3)

    monkeypatch.setattr(store.subprocess, "run", run)
    monkeypatch.setattr(store.shutil, "disk_usage", usage)
    store.report()
    out = capsys.readouterr().out
    assert out == "Image store: 8.0 GiB free\nLayer temporary storage: 7.0 GiB free\n"
    assert asked == ["private-store", temporary or "/var/tmp"]
    assert commands == [["podman", "info", "--format", "{{.Store.GraphRoot}}"], ["podman", "system", "df"]]


def test_removal_targets_only_this_distributions_labelled_image_then_prunes_even_after_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    commands = []

    def run(command: list[str], **options: object) -> None:
        assert command[:4] == ["bash", "-o", "pipefail", "-c"]
        commands.append(shlex.split(command[-1]))
        if "image" in commands[-1] and "rm" in commands[-1]:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(store.subprocess, "run", run)
    store.remove("debian-12")
    stopped, labelled, prune = commands
    assert stopped == [
        "podman", "ps", "--all", "--external", "--no-trunc", "--format",
        '{{if eq .State "storage"}}{{.ID}}{{end}}', "|", "xargs", "--no-run-if-empty", "podman", "rm", "--force",
    ]
    assert labelled == [
        "podman", "image", "ls", "--quiet", "--filter", f"label={store.IMAGE_LABEL}",
        "--filter", "reference=agentic-hil-bench-tier:debian-12", "|", "xargs", "--no-run-if-empty", "podman", "image", "rm",
    ]
    assert prune == ["podman", "image", "prune", "--force"]
    assert "::warning::Image cleanup failed" in capsys.readouterr().out


@pytest.mark.parametrize("phase", ["before", "after"])
def test_the_shared_run_lock_covers_removal_only_and_is_released_on_failure(
    monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    events = []

    class Lock:
        held = False

        def acquire(self) -> None:
            self.held = True

        def release(self) -> None:
            self.held = False

    lock = Lock()

    def create_lock(**options: object) -> Lock:
        assert options["tool"] == "tools/bench_image_store.py"
        assert options["runs_for"] == "a minute or two"
        assert callable(options["announce"])
        return lock

    monkeypatch.setattr(store, "RunLock", create_lock)

    def remove(distribution: str) -> None:
        events.append(("remove", lock.held))

    def report() -> None:
        events.append(("report", lock.held))
        raise OSError("scripted measurement failure")

    monkeypatch.setattr(store, "remove", remove)
    monkeypatch.setattr(store, "report", report)
    assert store.main([phase, "--distribution", "debian-12"]) == 1
    assert events == ([("remove", True), ("report", True)] if phase == "after" else [("report", False)])
    assert not lock.held
