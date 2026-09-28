from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from tests.bench import pyocd_recordings


def test_pyocd_metadata_commands_use_the_explicit_bench_environment(monkeypatch) -> None:
    environment = {
        "HOME": "/bench-home",
        "PATH": "/work/.venv/bin:/usr/bin:/bin",
        "AGENTIC_HIL_CONFIG": "/bench-config/config.yaml",
    }
    target_document = {
        "pyocd_version": "0.45.1",
        "targets": [
            {
                "name": "stm32f446retx",
                "part_number": "STM32F446RETx",
                "source": "pack",
            }
        ],
    }
    responses = iter(
        (
            SimpleNamespace(returncode=0, stdout=json.dumps(target_document), stderr=""),
            SimpleNamespace(returncode=0, stdout="Keil.STM32F4xx_DFP 3.1.1\n", stderr=""),
        )
    )
    calls: list[dict] = []
    monkeypatch.setattr(pyocd_recordings.shutil, "which", lambda _name: "/work/.venv/bin/pyocd")
    monkeypatch.setattr(
        pyocd_recordings.subprocess,
        "run",
        lambda *args, **kwargs: (calls.append(kwargs) or next(responses)),
    )

    executable, pyocd_version, pack_version = pyocd_recordings.pyocd_provenance(environment)

    assert (executable, pyocd_version, pack_version) == ("/work/.venv/bin/pyocd", "0.45.1", "3.1.1")
    assert len(calls) == 2
    assert all(call.get("env") == environment for call in calls)


def test_pyocd_bench_environment_restores_the_image_pack_data_home() -> None:
    isolated_environment = {
        "HOME": "/bench-home",
        "XDG_DATA_HOME": "/tmp/pytest-home/.local/share",
        "XDG_CONFIG_HOME": "/bench-config",
        "XDG_STATE_HOME": "/bench-state",
    }
    bench = SimpleNamespace(environment=lambda **_overrides: isolated_environment.copy())

    environment = pyocd_recordings.pyocd_bench_environment(bench)
    server_environment = pyocd_recordings.PyOcdBench(bench).environment()
    expected_data_home = str(Path(isolated_environment["HOME"]) / ".local" / "share")

    assert environment["HOME"] == "/bench-home"
    assert environment["XDG_DATA_HOME"] == expected_data_home
    assert server_environment["XDG_DATA_HOME"] == expected_data_home
    assert environment["XDG_CONFIG_HOME"] == "/bench-config"
    assert environment["XDG_STATE_HOME"] == "/bench-state"
    assert isolated_environment["XDG_DATA_HOME"] == "/tmp/pytest-home/.local/share"
