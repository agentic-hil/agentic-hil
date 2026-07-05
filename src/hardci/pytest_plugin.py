"""pytest plugin exposing HardCI as fixtures for hardware-in-the-loop test suites.

Usage in a firmware project with a `.hardci/config.yaml`:

    def test_open_sensor_diagnosis(hardci):
        started = hardci.call("hardci_adapter_session_start", {"adapter_id": "ntc_sim"})
        assert started["ok"] is True

Tests using the fixtures are skipped when no HardCI configuration file exists,
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

    from hardci.tools import HardCIToolService
    from hardci.types import HardCIConfig

# Mirrors hardci.config.DEFAULT_CONFIG_PATH; kept as a literal so this module
# stays import-light — pytest imports every installed pytest11 entry point on
# startup, and hardci.config would pull in yaml + jsonschema for unrelated runs.
DEFAULT_CONFIG_PATH = ".hardci/config.yaml"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("hardci")
    group.addoption(
        "--hardci-config",
        action="store",
        default=None,
        help=f"Path to the HardCI project configuration (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.addini("hardci_config", help="Path to the HardCI project configuration.", default=None)


def resolve_plugin_config_path(config: pytest.Config) -> str:
    option = config.getoption("--hardci-config")
    if option:
        return str(option)  # command-line paths stay relative to the invocation cwd
    ini_value = config.getini("hardci_config")
    if ini_value:
        return rootdir_anchored(config, str(ini_value))
    return rootdir_anchored(config, DEFAULT_CONFIG_PATH)


def rootdir_anchored(config: pytest.Config, path: str) -> str:
    return path if Path(path).is_absolute() else str(config.rootpath / path)


@pytest.fixture(scope="session")
def hardci_config(request: pytest.FixtureRequest) -> HardCIConfig:
    """The validated HardCI project configuration.

    Skips when the configuration file does not exist; fails when it exists but
    is unreadable or invalid.
    """
    from hardci.config import ConfigError, load_config

    config_path = resolve_plugin_config_path(request.config)
    try:
        return load_config(config_path, work_dir=str(request.config.rootpath))
    except ConfigError as error:
        if error.error_type == "config_file_not_found":
            pytest.skip(f"HardCI configuration unavailable: {error.summary} [path: {config_path}]")
        pytest.fail(f"HardCI configuration invalid ({error.error_type}): {error.summary} [path: {config_path}]", pytrace=False)


@pytest.fixture(scope="session")
def _hardci_service(hardci_config: HardCIConfig) -> Iterator[HardCIToolService]:
    from hardci.tools import HardCIToolService

    service = HardCIToolService(hardci_config)
    try:
        yield service
    finally:
        service.close()


@pytest.fixture()
def hardci(_hardci_service: HardCIToolService) -> Iterator[HardCIToolService]:
    """A ready HardCIToolService; call tools by name exactly like an MCP agent would.

    The service (config, debugger backend) is shared across the session, but
    adapter, COM, and CAN sessions opened during a test are stopped afterwards
    so injected faults and stimulus state cannot leak between tests.
    """
    try:
        yield _hardci_service
    finally:
        _hardci_service.adapters.close()
        _hardci_service.com_ports.close()
        _hardci_service.can_buses.close()
