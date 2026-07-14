"""pytest plugin exposing Agentic HIL fixtures for hardware-in-the-loop test suites.

Usage in a firmware project with a `.agentic-hil/config.yaml`:

    def test_open_sensor_diagnosis(agentic_hil):
        started = agentic_hil.call("adapter_session_start", {"adapter_id": "ntc_sim"})
        assert started["ok"] is True

Tests using the fixtures are skipped when no Agentic HIL configuration file exists,
so suites stay green on machines without a hardware setup. An existing but
invalid configuration fails loudly instead — a typo must not silently disable
the hardware suite in CI.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentic_hil.tools import AgenticHILToolService
    from agentic_hil.types import AgenticHILConfig

# Mirrors agentic_hil.config.DEFAULT_CONFIG_PATH; kept as a literal so this module
# stays import-light — pytest imports every installed pytest11 entry point on
# startup, and agentic_hil.config would pull in yaml + jsonschema for unrelated runs.
DEFAULT_CONFIG_PATH = ".agentic-hil/config.yaml"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("agentic_hil")
    group.addoption(
        "--agentic-hil-config",
        action="store",
        default=None,
        help=f"Path to the Agentic Hardware-in-the-Loop (Agentic HIL) project configuration (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.addini("agentic_hil_config", help="Path to the Agentic HIL project configuration.", default=None)


def resolve_plugin_config_path(config: pytest.Config) -> str:
    option = config.getoption("--agentic-hil-config")
    if option:
        return str(Path(str(option)).resolve())  # command-line paths stay relative to the invocation cwd
    ini_value = config.getini("agentic_hil_config")
    if ini_value:
        return rootdir_anchored(config, str(ini_value))
    return rootdir_anchored(config, DEFAULT_CONFIG_PATH)


def rootdir_anchored(config: pytest.Config, path: str) -> str:
    return path if Path(path).is_absolute() else str(config.rootpath / path)


@pytest.fixture(scope="session")
def agentic_hil_config(request: pytest.FixtureRequest) -> AgenticHILConfig:
    """The validated Agentic HIL project configuration.

    Skips when the configuration file does not exist; fails when it exists but
    is unreadable or invalid.
    """
    from agentic_hil.config import ConfigError, load_config

    config_path = resolve_plugin_config_path(request.config)
    try:
        return load_config(config_path, work_dir=str(request.config.rootpath))
    except ConfigError as error:
        if error.error_type == "config_file_not_found":
            pytest.skip(f"Agentic HIL configuration unavailable: {error.summary} [path: {config_path}]")
        pytest.fail(
            f"Agentic HIL configuration invalid ({error.error_type}): {error.summary} [path: {config_path}]",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def _agentic_hil_service(agentic_hil_config: AgenticHILConfig) -> Iterator[AgenticHILToolService]:
    from agentic_hil.tools import AgenticHILToolService

    service = AgenticHILToolService(agentic_hil_config)
    try:
        yield service
    finally:
        service.close()


@pytest.fixture()
def agentic_hil(_agentic_hil_service: AgenticHILToolService) -> Iterator[AgenticHILToolService]:
    """A ready AgenticHILToolService; call tools by name exactly like an MCP agent would.

    The service (config, debugger backend) is shared across the session, but
    adapter, COM, and CAN sessions opened during a test are stopped afterwards
    so injected faults and stimulus state cannot leak between tests.
    """
    try:
        yield _agentic_hil_service
    finally:
        _agentic_hil_service.adapters.close()
        _agentic_hil_service.com_ports.close()
        _agentic_hil_service.can_buses.close()
