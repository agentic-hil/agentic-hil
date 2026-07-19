"""pytest plugin exposing Agentic HIL fixtures for hardware-in-the-loop test suites.

Usage in a configured firmware project:

    def test_target_is_available(agentic_hil):
        result = agentic_hil.call("probe_target")
        assert result["ok"] is True

Tests using the fixtures are skipped when neither the canonical external config nor
an ``AGENTIC_HIL_CONFIG`` override exists. An invalid configuration fails loudly
instead, so a typo cannot silently disable the hardware suite in CI.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentic_hil.report import overall_success

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentic_hil.tools import AgenticHILToolService
    from agentic_hil.types import AgenticHILConfig

CONFIG_ENV = "AGENTIC_HIL_CONFIG"
DEFAULT_CONFIG_PATH = ".agentic-hil/config.yaml"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("agentic_hil")
    group.addoption(
        "--agentic-hil-config",
        action="store",
        default=None,
        help="Deprecated config selector; must resolve to the discovered authoritative config.",
    )
    parser.addini("agentic_hil_config", help="Deprecated Agentic HIL config selector.", default=None)


def resolve_plugin_config_path(config: pytest.Config) -> str:
    option = config.getoption("--agentic-hil-config")
    if option:
        return str(Path(str(option)).resolve())
    ini_value = config.getini("agentic_hil_config")
    if ini_value:
        return rootdir_anchored(config, str(ini_value))
    return rootdir_anchored(config, DEFAULT_CONFIG_PATH)


def rootdir_anchored(config: pytest.Config, path: str) -> str:
    return path if Path(path).is_absolute() else str(config.rootpath / path)


def configured_legacy_selector(config: pytest.Config) -> str | None:
    option = config.getoption("--agentic-hil-config")
    if option:
        return str(Path(str(option)).resolve())
    ini_value = config.getini("agentic_hil_config")
    return rootdir_anchored(config, str(ini_value)) if ini_value else None


@pytest.fixture(scope="session")
def agentic_hil_config(request: pytest.FixtureRequest) -> AgenticHILConfig:
    """The validated Agentic HIL project configuration.

    Skips when no config exists; fails when an available config is invalid.
    """
    from agentic_hil.config import ConfigError, load_authoritative_config, project_config_path

    authoritative_path = Path(os.environ.get(CONFIG_ENV) or project_config_path(request.config.rootpath)).resolve()
    legacy_selector = configured_legacy_selector(request.config)
    if legacy_selector is not None and Path(legacy_selector).resolve() != authoritative_path:
        pytest.fail(
            "Deprecated Agentic HIL config selector cannot change policy authority. "
            f"Set {CONFIG_ENV} to the absolute external config path or remove the legacy option.",
            pytrace=False,
        )
    if not os.environ.get(CONFIG_ENV) and not authoritative_path.is_file():
        pytest.skip("Agentic HIL configuration unavailable: canonical external config does not exist")

    try:
        return load_authoritative_config(request.config.rootpath)
    except ConfigError as error:
        pytest.fail(
            f"Agentic HIL configuration invalid ({error.error_type}): {error.summary}",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def _agentic_hil_service(agentic_hil_config: AgenticHILConfig) -> Iterator[AgenticHILToolService]:
    from agentic_hil.tools import AgenticHILToolService

    service = AgenticHILToolService(agentic_hil_config, frontend="pytest")
    try:
        yield service
    finally:
        service.close()


@pytest.fixture()
def agentic_hil(_agentic_hil_service: AgenticHILToolService) -> Iterator[AgenticHILToolService]:
    """A ready AgenticHILToolService; call tools by name exactly like an MCP agent would.

    The service (config, debugger backend) is shared across the session, but
    Debug, COM, and CAN sessions opened during a test are stopped afterwards so
    state cannot leak between tests.
    """
    try:
        yield _agentic_hil_service
    finally:
        errors: list[str] = []
        interrupt: BaseException | None = None
        if _agentic_hil_service._debug_artifact is not None:
            try:
                stopped = _agentic_hil_service.call("debug_stop_session")
                if not overall_success(stopped):
                    errors.append(f"debug: {stopped.get('summary', stopped.get('error_type'))}")
            except BaseException as error:
                errors.append(f"debug: {type(error).__name__}: {error}")
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interrupt = error
        for name, resource in [("COM", _agentic_hil_service.com_ports), ("CAN", _agentic_hil_service.can_buses), ("adapter", _agentic_hil_service.adapters)]:
            try:
                resource.close()
            except BaseException as error:
                errors.append(f"{name}: {type(error).__name__}: {error}")
                if interrupt is None and isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interrupt = error
        if interrupt is not None:
            interrupt.args = (*interrupt.args, "Cleanup errors: " + "; ".join(errors))
            raise interrupt
        if errors:
            pytest.fail("Agentic HIL fixture cleanup failed: " + "; ".join(errors), pytrace=False)
